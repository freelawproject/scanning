"""Tests for the YOLO detection stage: switches, rows, payload, sweep.

Short on purpose. Everything that separates an asynchronous provider
from doctor -- the claim, the poll, the deadlines, the cancel, the
retry -- is shared with dots.mocr and covered in ``test_dots_mocr.py``.
What is tested here is what detection does *differently*: its own
endpoint, its own caps, its own payload, and the sweep that starts one
run per shard set (#250).

No HTTP and no S3 -- ``runpod_client`` and the S3 helpers are patched.
"""

from unittest.mock import patch

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from scanning import jobs, yolo
from scanning.factories import ScanFactory
from scanning.models import (
    ExternalJob,
    JobEngine,
    JobProvider,
    JobStage,
    JobStatus,
    Scan,
    Status,
)
from scanning.tests.test_jobs import make_manifest
from scanning.tests.test_views import ScanningTestCase

YOLO = {
    "RUNPOD_ENABLED": True,
    "RUNPOD_API_KEY": "key-1",
    "RUNPOD_PRESIGNED_TTL": 3600,
    "RUNPOD_REQUEST_TIMEOUT": 600,
    "YOLO_ENABLED": True,
    "RUNPOD_YOLO_ENDPOINT_ID": "ep-yolo",
    "YOLO_MAX_CONCURRENCY": 4,
    "YOLO_MAX_ATTEMPTS": 3,
    "YOLO_SECONDS_PER_PAGE": 2.0,
    # The other engines off, so a tick exercises this wave alone.
    "DOTS_MOCR_ENABLED": False,
    "DOCTOR_ENABLED": False,
}


def detect_jobs(scan):
    """Return a scan's detection rows in shard order.

    :param scan: The scan to look up.
    :returns: Its rows.
    :rtype: list[ExternalJob]
    """
    return list(
        ExternalJob.objects.filter(
            scan=scan,
            stage=JobStage.DETECT,
            engine=JobEngine.BLACKLETTER,
        ).order_by("shard_index")
    )


# ── switches ────────────────────────────────────────────────────────
class TestEnabled(ScanningTestCase):
    """Both the stage switch and the RunPod credentials are required."""

    @override_settings(**YOLO)
    def test_on_with_everything_set(self):
        self.assertTrue(yolo.enabled())

    @override_settings(**{**YOLO, "YOLO_ENABLED": False})
    def test_off_without_the_stage_switch(self):
        self.assertFalse(yolo.enabled())

    @override_settings(**{**YOLO, "RUNPOD_YOLO_ENDPOINT_ID": ""})
    def test_off_without_this_engine_s_endpoint(self):
        # Each engine is its own endpoint. An unset one turns this
        # engine off and must leave the other alone.
        self.assertFalse(yolo.enabled())

    @override_settings(**{**YOLO, "RUNPOD_ENABLED": False})
    def test_off_when_runpod_is_off_for_the_environment(self):
        self.assertFalse(yolo.enabled())

    @override_settings(**{**YOLO, "DOTS_MOCR_ENABLED": False})
    def test_one_engine_off_does_not_turn_the_other_off(self):
        from scanning import dots_mocr

        self.assertFalse(dots_mocr.enabled())
        self.assertTrue(yolo.enabled())


# ── creating the work ───────────────────────────────────────────────
class TestEnsureDetectJobs(ScanningTestCase):
    """One row per shard of the original, at DETECT/BLACKLETTER/RUNPOD."""

    def test_one_row_per_shard_addressing_its_own_pages(self):
        scan = ScanFactory()
        created = yolo.ensure_detect_jobs(
            scan, make_manifest(shard_count=3, pages_per_shard=10)
        )

        self.assertEqual(len(created), 3)
        for index, job in enumerate(created):
            self.assertEqual(job.stage, JobStage.DETECT)
            self.assertEqual(job.engine, JobEngine.BLACKLETTER)
            self.assertEqual(job.provider, JobProvider.RUNPOD)
            self.assertEqual(job.status, JobStatus.PENDING)
            self.assertEqual(job.shard_index, index)
            self.assertEqual(job.input_manifest["from_page"], index * 10)

    def test_the_input_is_the_original_shard_not_the_bitonal_copy(self):
        # bl-warm was trained on greyscale renders; its large region
        # classes collapse on 1-bit pages (#167).
        scan = ScanFactory()
        created = yolo.ensure_detect_jobs(scan, make_manifest(shard_count=1))
        self.assertIn("shards/", created[0].input_key)
        self.assertNotIn("bitonal", created[0].input_key)

    def test_a_second_call_reuses_the_run(self):
        scan = ScanFactory()
        manifest = make_manifest(shard_count=2)
        first = yolo.ensure_detect_jobs(scan, manifest)
        second = yolo.ensure_detect_jobs(scan, manifest)

        self.assertEqual([row.pk for row in first], [row.pk for row in second])
        self.assertEqual(len(detect_jobs(scan)), 2)

    def test_a_dead_row_starts_a_new_run(self):
        scan = ScanFactory()
        manifest = make_manifest(shard_count=2)
        rows = yolo.ensure_detect_jobs(scan, manifest)
        ExternalJob.objects.filter(pk=rows[0].pk).update(
            status=JobStatus.FAILED
        )

        with patch("scanning.s3_sync.s3_active", return_value=False):
            fresh = yolo.ensure_detect_jobs(scan, manifest)

        self.assertEqual(fresh[0].run, 2)

    def test_an_unchanged_shard_is_carried_into_a_replacement_run(self):
        # Detection results are kept for #196, so a replacement run may
        # carry them. Re-detecting a shard already detected is a second
        # payment for output already in S3.
        scan = ScanFactory()
        manifest = make_manifest(shard_count=2, pages_per_shard=1)
        rows = yolo.ensure_detect_jobs(scan, manifest)
        ExternalJob.objects.filter(pk=rows[0].pk).update(
            status=JobStatus.COMPLETED,
            result_key="jobs/detect/blackletter/r1-s0-a1.json",
            completed_at=timezone.now(),
        )
        ExternalJob.objects.filter(pk=rows[1].pk).update(
            status=JobStatus.FAILED
        )

        with (
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch("scanning.s3_sync.object_exists", return_value=True),
        ):
            fresh = yolo.ensure_detect_jobs(scan, manifest)

        self.assertEqual(fresh[0].status, JobStatus.COMPLETED)
        self.assertEqual(
            fresh[0].result_key, "jobs/detect/blackletter/r1-s0-a1.json"
        )
        # A carried row has no provider job, so every cancel path stays
        # a no-op on it.
        self.assertEqual(fresh[0].external_id, "")
        self.assertEqual(fresh[1].status, JobStatus.PENDING)


# ── the payload ─────────────────────────────────────────────────────
class TestBuildPayload(ScanningTestCase):
    """What the worker of #194 is handed."""

    def _job(self, **fields):
        scan = ScanFactory()
        job = yolo.ensure_detect_jobs(scan, make_manifest(shard_count=1))[0]
        if fields:
            for name, value in fields.items():
                setattr(job, name, value)
        job.result_key = "jobs/detect/blackletter/r1-s0-a1.json"
        return job

    def test_the_shape_the_handler_reads(self):
        job = self._job()
        payload = yolo.build_payload(job, "https://s3/in", "https://s3/out")

        self.assertEqual(payload["action"], "detect")
        self.assertEqual(payload["scan_pk"], job.scan_id)
        self.assertEqual(payload["pdf_url"], "https://s3/in")
        self.assertEqual(payload["result_url"], "https://s3/out")
        self.assertEqual(payload["result_key"], job.result_key)
        self.assertEqual(payload["models"], ["bl_warm"])
        self.assertEqual(payload["confidence"], 0.20)

    def test_no_dpi_and_no_max_pages_are_sent(self):
        # blackletter fixes the render resolution at 200, matching the
        # rest of the corpus, and the worker enforces its own page
        # ceiling. Neither is ours to set.
        payload = yolo.build_payload(self._job(), "in", "out")
        self.assertNotIn("dpi", payload)
        self.assertNotIn("max_pages", payload)

    def test_a_row_may_override_the_tuning_keys(self):
        # An experiment needs no deploy: the override rides on the row.
        job = self._job()
        job.input_manifest = {
            **job.input_manifest,
            "models": ["small"],
            "confidence": 0.5,
        }
        payload = yolo.build_payload(job, "in", "out")

        self.assertEqual(payload["models"], ["small"])
        self.assertEqual(payload["confidence"], 0.5)

    def test_the_module_constant_is_not_mutated_by_a_caller(self):
        payload = yolo.build_payload(self._job(), "in", "out")
        payload["models"].append("medium")
        self.assertEqual(yolo.MODELS, ["bl_warm"])


# ── the wave ────────────────────────────────────────────────────────
@override_settings(**YOLO)
class TestSubmitWave(ScanningTestCase):
    """Detection submits with its own payload, to its own endpoint."""

    def _tick(self):
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
            patch(
                "scanning.runpod_client.endpoint_config",
                return_value=("https://api/v2/ep-yolo", {}),
            ) as config,
        ):
            jobs.submit_pending()
        return submit, config

    def test_a_pending_row_is_sent_with_the_detect_payload(self):
        scan = ScanFactory()
        yolo.ensure_detect_jobs(scan, make_manifest(shard_count=1))

        submit, config = self._tick()

        self.assertEqual(submit.call_count, 1)
        payload = submit.call_args[0][2]
        self.assertEqual(payload["action"], "detect")
        self.assertEqual(payload["models"], ["bl_warm"])
        # Its own endpoint, not the other engine's.
        config.assert_called_with("ep-yolo")
        self.assertEqual(detect_jobs(scan)[0].status, JobStatus.SUBMITTED)

    def test_a_disabled_engine_sends_nothing(self):
        scan = ScanFactory()
        yolo.ensure_detect_jobs(scan, make_manifest(shard_count=1))

        with override_settings(YOLO_ENABLED=False):
            submit, _ = self._tick()

        submit.assert_not_called()
        self.assertEqual(detect_jobs(scan)[0].status, JobStatus.PENDING)

    def test_one_engine_off_does_not_hold_up_the_other(self):
        # Each engine counts its own rows against its own cap, so a
        # stage switched off must not stop the one still running.
        from scanning import dots_mocr

        scan = ScanFactory()
        manifest = make_manifest(shard_count=1)
        dots_mocr.ensure_analyze_jobs(scan, manifest)
        yolo.ensure_detect_jobs(scan, manifest)

        submit, _ = self._tick()

        self.assertEqual(submit.call_count, 1)
        self.assertEqual(submit.call_args[0][2]["action"], "detect")

    def test_the_cap_is_this_engine_s_own(self):
        scan = ScanFactory()
        yolo.ensure_detect_jobs(scan, make_manifest(shard_count=4))

        with override_settings(YOLO_MAX_CONCURRENCY=2):
            submit, _ = self._tick()

        self.assertEqual(submit.call_count, 2)


# ── the per-engine knobs ────────────────────────────────────────────
@override_settings(**YOLO)
class TestPerEngineKnobs(ScanningTestCase):
    """A detection row must not take the other engine's limits.

    This is the regression the engine table exists to prevent: before
    it, every RunPod row read ``DOTS_MOCR_MAX_ATTEMPTS`` and
    ``DOTS_MOCR_SECONDS_PER_PAGE``.
    """

    def _row(self, engine=JobEngine.BLACKLETTER):
        scan = ScanFactory()
        if engine == JobEngine.BLACKLETTER:
            return yolo.ensure_detect_jobs(
                scan, make_manifest(shard_count=1, pages_per_shard=10)
            )[0]
        from scanning import dots_mocr

        return dots_mocr.ensure_analyze_jobs(
            scan, make_manifest(shard_count=1, pages_per_shard=10)
        )[0]

    @override_settings(YOLO_MAX_ATTEMPTS=7, DOTS_MOCR_MAX_ATTEMPTS=2)
    def test_the_attempt_cap_is_this_engine_s_own(self):
        self.assertEqual(jobs._max_attempts(self._row()), 7)
        self.assertEqual(jobs._max_attempts(self._row(JobEngine.DOTS_MOCR)), 2)

    @override_settings(
        RUNPOD_REQUEST_TIMEOUT=100,
        YOLO_SECONDS_PER_PAGE=1.0,
        DOTS_MOCR_SECONDS_PER_PAGE=5.0,
    )
    def test_the_per_page_allowance_is_this_engine_s_own(self):
        now = timezone.now()
        # 100 + 10 pages x 1.0 second, against 100 + 10 x 5.0.
        self.assertEqual(
            jobs.runpod_execution_deadline(self._row(), now) - now,
            timezone.timedelta(seconds=110),
        )
        self.assertEqual(
            jobs.runpod_execution_deadline(self._row(JobEngine.DOTS_MOCR), now)
            - now,
            timezone.timedelta(seconds=150),
        )

    def test_the_endpoint_is_this_engine_s_own(self):
        self.assertEqual(jobs._runpod_endpoint(self._row()), "ep-yolo")

    def test_an_engine_with_no_table_entry_is_refused(self):
        # Silently taking another engine's limits is the failure this
        # replaces. Only the ensure_* wrappers create rows, so this is
        # an internal fault, and it reads as an unconfigured endpoint.
        row = self._row()
        row.engine = JobEngine.SURYA
        with self.assertRaises(jobs.UnknownRunpodEngine):
            jobs._runpod_endpoint(row)


# ── the sweep ───────────────────────────────────────────────────────
FINGERPRINT = "3072:30"


def sweep_manifest(shard_count=3, pages_per_shard=10):
    """A manifest whose source fingerprint is :data:`FINGERPRINT`.

    ``make_manifest`` sizes the source at 1024 bytes per shard, so three
    shards of ten pages give ``3072:30``.

    :param shard_count: How many shards to describe.
    :param pages_per_shard: Pages in each shard.
    :returns: A manifest dict.
    :rtype: dict
    """
    return make_manifest(shard_count, pages_per_shard)


class TestEnqueueMissingRuns(ScanningTestCase):
    """One run per shard set, started by the daemon, and never a second.

    The rule the sweep enforces (#250): a scan whose current shard set
    (``Scan.source_fingerprint``) has no detection row -- alive or dead
    -- gets exactly one run. Everything else is left alone.
    """

    def setUp(self):
        super().setUp()
        self.manifest = sweep_manifest()

    def _scan(
        self, status=Status.AWAITING_VALIDATION, fingerprint=FINGERPRINT
    ):
        return ScanFactory(
            page_count=30, status=status, source_fingerprint=fingerprint
        )

    _UNSET = object()

    def _sweep(self, manifest=_UNSET, reason=""):
        """Run the sweep with S3 on and the manifest check answered."""
        with (
            override_settings(**YOLO),
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch(
                "scanning.sharding.committed_manifest",
                return_value=(
                    self.manifest if manifest is self._UNSET else manifest,
                    reason,
                ),
            ) as committed,
            patch("scanning.runpod_client.submit_job") as submit,
        ):
            started = yolo.enqueue_missing_runs()
        self.committed = committed
        self.submit = submit
        return started

    def test_a_fingerprinted_scan_with_no_run_gets_one_row_per_shard(self):
        for status in sorted(yolo.SWEEP_STATUSES):
            with self.subTest(status=status):
                scan = self._scan(status=status)
                self.assertEqual(self._sweep(), 1)
                rows = detect_jobs(scan)
                self.assertEqual(len(rows), 3)
                self.assertEqual(
                    {row.status for row in rows}, {JobStatus.PENDING}
                )
                self.assertEqual(
                    {row.source_fingerprint for row in rows}, {FINGERPRINT}
                )

    def test_the_sweep_makes_no_call_to_runpod(self):
        self._scan()
        self._sweep()
        self.submit.assert_not_called()

    def test_a_second_tick_starts_nothing(self):
        scan = self._scan()
        self.assertEqual(self._sweep(), 1)
        self.assertEqual(self._sweep(), 0)
        self.assertEqual(len(detect_jobs(scan)), 3)

    def test_a_live_run_over_the_same_set_is_left_alone(self):
        scan = self._scan()
        yolo.ensure_detect_jobs(scan, self.manifest)
        self.assertEqual(self._sweep(), 0)
        self.committed.assert_not_called()

    def test_a_dead_run_over_the_same_set_is_not_re_run(self):
        """A FAILED row means the attempts are spent. A fourth is a
        staff decision, not a tick."""
        scan = self._scan()
        rows = yolo.ensure_detect_jobs(scan, self.manifest)
        ExternalJob.objects.filter(pk=rows[1].pk).update(
            status=JobStatus.FAILED
        )
        self.assertEqual(self._sweep(), 0)
        self.assertEqual(len(detect_jobs(scan)), 3)

    def test_a_consumed_run_over_the_same_set_is_left_alone(self):
        scan = self._scan()
        rows = yolo.ensure_detect_jobs(scan, self.manifest)
        ExternalJob.objects.filter(pk__in=[r.pk for r in rows]).update(
            status=JobStatus.CONSUMED
        )
        self.assertEqual(self._sweep(), 0)

    def test_rows_with_a_blank_fingerprint_match_anything(self):
        """A button-era run (before the column) is over the current set
        unless the original was re-cut, and re-cutting stamps every
        later row. Never re-pay it."""
        scan = self._scan()
        rows = yolo.ensure_detect_jobs(scan, self.manifest)
        ExternalJob.objects.filter(pk__in=[r.pk for r in rows]).update(
            source_fingerprint=""
        )
        self.assertEqual(self._sweep(), 0)

    def test_a_re_cut_set_gets_a_new_run_and_carries_unchanged_shards(self):
        scan = self._scan(fingerprint="2048:20")
        old_manifest = make_manifest(shard_count=2, pages_per_shard=10)
        old = yolo.ensure_detect_jobs(scan, old_manifest)
        self.assertEqual(old[0].source_fingerprint, "2048:20")
        for index, row in enumerate(old):
            ExternalJob.objects.filter(pk=row.pk).update(
                status=JobStatus.CONSUMED,
                result_key=f"jobs/detect/blackletter/r1-s{index}-a1.json",
                completed_at=timezone.now(),
            )
        # The original was re-uploaded: shard 1 changed, shard 0 did not.
        new_manifest = make_manifest(shard_count=2, pages_per_shard=10)
        new_manifest["shards"][1]["size_bytes"] = 4096
        new_manifest["source"]["size_bytes"] = 1024 + 4096
        Scan.objects.filter(pk=scan.pk).update(source_fingerprint="5120:20")
        self.manifest = new_manifest

        with patch("scanning.s3_sync.object_exists", return_value=True):
            self.assertEqual(self._sweep(), 1)

        fresh = yolo.live_detect_jobs(scan)
        self.assertEqual(fresh[0].run, 2)
        self.assertEqual(
            {row.source_fingerprint for row in fresh}, {"5120:20"}
        )
        self.assertEqual(fresh[0].status, JobStatus.COMPLETED)
        self.assertEqual(
            fresh[0].provider_meta["carried_from"]["job"], old[0].pk
        )
        self.assertEqual(fresh[1].status, JobStatus.PENDING)

    def test_only_the_parked_review_statuses_are_swept(self):
        for status in (
            Status.UPLOADED,
            Status.QUEUED,
            Status.PROCESSING,
            Status.ERROR,
            Status.CANCELLED,
            Status.APPROVED,
            Status.PENDING_REVIEW,
        ):
            with self.subTest(status=status):
                scan = self._scan(status=status)
                self.assertEqual(self._sweep(), 0)
                self.assertEqual(detect_jobs(scan), [])

    def test_a_scan_with_no_shard_set_is_not_swept(self):
        self._scan(fingerprint="")
        self.assertEqual(self._sweep(), 0)
        self.committed.assert_not_called()

    def test_a_refused_manifest_is_skipped_and_logged(self):
        scan = self._scan()
        with self.assertLogs("scanning.yolo", level="INFO") as logs:
            started = self._sweep(
                manifest=None, reason="The original PDF has changed"
            )
        self.assertEqual(started, 0)
        self.assertEqual(detect_jobs(scan), [])
        self.assertIn("The original PDF has changed", "".join(logs.output))

    def test_the_stage_switch_stops_the_sweep(self):
        scan = self._scan()
        with (
            override_settings(**{**YOLO, "YOLO_ENABLED": False}),
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch("scanning.sharding.committed_manifest") as committed,
        ):
            self.assertEqual(yolo.enqueue_missing_runs(), 0)
        committed.assert_not_called()
        self.assertEqual(detect_jobs(scan), [])

    def test_no_s3_no_sweep(self):
        scan = self._scan()
        with (
            override_settings(**YOLO),
            patch("scanning.s3_sync.s3_active", return_value=False),
            patch("scanning.sharding.committed_manifest") as committed,
        ):
            self.assertEqual(yolo.enqueue_missing_runs(), 0)
        committed.assert_not_called()
        self.assertEqual(detect_jobs(scan), [])

    def test_the_sweep_writes_no_scan_status(self):
        scan = self._scan(status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW)
        self._sweep()
        scan.refresh_from_db()
        self.assertEqual(
            scan.status, Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )


# ── what the viewer shows ───────────────────────────────────────────
class TestViewerShowsTheRun(ScanningTestCase):
    """The stage writes no scan status, so the rows are the only place a
    viewer can see it happening -- and there is no button to start it
    (#250)."""

    def setUp(self):
        super().setUp()
        self.staff = self.make_staff_user()
        self.scan = ScanFactory(
            page_count=30, status=Status.PAGE_COMPLETENESS_REVIEW_DONE
        )

    def _bar(self):
        self.client.force_login(self.staff)
        response = self.client.get(
            reverse("process_actions", kwargs={"pk": self.scan.pk})
        )
        return response.json()["html"]

    def test_the_run_shows_on_the_process_page(self):
        yolo.ensure_detect_jobs(self.scan, make_manifest(shard_count=3))
        self.assertIn("Detection running", self._bar())

    def test_there_is_no_start_button(self):
        from django.urls import NoReverseMatch

        html = self._bar()
        self.assertNotIn("Run detection", html)
        self.assertNotIn("start-yolo", html)
        with self.assertRaises(NoReverseMatch):
            reverse("start_yolo_detect", kwargs={"pk": self.scan.pk})

    def test_next_detect_says_where_the_run_stands(self):
        from scanning.views_process import NO_DETECTIONS_MESSAGE

        self.assertIn(NO_DETECTIONS_MESSAGE, self._bar())
        rows = yolo.ensure_detect_jobs(self.scan, make_manifest(shard_count=3))
        self.assertIn("Detection is running: 0 of 3", self._bar())
        ExternalJob.objects.filter(pk=rows[0].pk).update(
            status=JobStatus.FAILED, error_code="WORKER_DEAD"
        )
        self.assertIn("Detection failed on 1 of 3", self._bar())
        self.assertIn("WORKER_DEAD", self._bar())
        ExternalJob.objects.filter(pk__in=[r.pk for r in rows]).update(
            status=JobStatus.CONSUMED, error_code=""
        )
        self.assertIn("Detection finished", self._bar())


class TestDetectionMessage(ScanningTestCase):
    """The one text the flash and the button title share."""

    def _summary(self, **overrides):
        base = {
            "run": 1,
            "total": 3,
            "done": 0,
            "open": 3,
            "failed": 0,
            "error_code": "",
        }
        return {**base, **overrides}

    def test_no_run(self):
        from scanning.views_process import (
            NO_DETECTIONS_MESSAGE,
            detection_message,
        )

        self.assertEqual(detection_message(None), NO_DETECTIONS_MESSAGE)

    def test_a_failure_wins_over_progress_and_names_the_code(self):
        from scanning.views_process import detection_message

        text = detection_message(
            self._summary(open=1, failed=1, done=1, error_code="OOM")
        )
        self.assertIn("failed on 1 of 3", text)
        self.assertIn("OOM", text)

    def test_progress(self):
        from scanning.views_process import detection_message

        text = detection_message(self._summary(open=2, done=1))
        self.assertIn("running: 1 of 3", text)

    def test_finished_waits_for_the_approval(self):
        from scanning.views_process import detection_message

        text = detection_message(self._summary(open=0, done=3))
        self.assertIn("finished", text)
        self.assertIn("approval", text)

    def test_start_detect_flashes_the_same_text(self):
        user = self.make_user()
        scan = ScanFactory(
            page_count=30, status=Status.PAGE_COMPLETENESS_REVIEW_DONE
        )
        yolo.ensure_detect_jobs(scan, make_manifest(shard_count=3))
        self.client.force_login(user)
        response = self.client.post(
            reverse("start_detect", kwargs={"pk": scan.pk}), follow=True
        )
        flashed = [str(m) for m in response.context["messages"]]
        self.assertTrue(
            any("Detection is running: 0 of 3" in m for m in flashed),
            flashed,
        )
