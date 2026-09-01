"""Tests for the YOLO detection stage: switches, rows, payload, button.

Short on purpose. Everything that separates an asynchronous provider
from doctor -- the claim, the poll, the deadlines, the cancel, the
retry -- is shared with dots.mocr and covered in ``test_dots_mocr.py``.
What is tested here is what detection does *differently*: its own
endpoint, its own caps, its own payload, and a button that no pipeline
may replace.

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


# ── the start button ────────────────────────────────────────────────
@override_settings(**YOLO)
class TestStartButton(ScanningTestCase):
    """Staff-only, and it writes rows rather than calling RunPod."""

    def setUp(self):
        super().setUp()
        self.staff = self.make_staff_user()
        self.scan = ScanFactory(page_count=30)
        self.url = reverse("start_yolo_detect", kwargs={"pk": self.scan.pk})
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
        rows = detect_jobs(self.scan)
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

    def test_the_request_never_cuts_shards(self):
        # A stale set is refused, not re-cut: re-cutting reads a
        # multi-gigabyte original, which is the pipeline's work.
        with (
            self._committed(),
            patch("scanning.sharding.ensure_shards") as cut,
        ):
            self._press()
        cut.assert_not_called()

    def test_a_non_staff_user_is_refused(self):
        with self._committed():
            response = self._press(self.make_user())
        self.assertEqual(detect_jobs(self.scan), [])
        messages = list(response.wsgi_request._messages)
        self.assertIn("Only staff", str(messages[0]))

    def test_an_anonymous_user_is_redirected_to_login(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])
        self.assertEqual(detect_jobs(self.scan), [])

    def test_a_get_is_rejected(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_a_disabled_stage_is_refused(self):
        with override_settings(YOLO_ENABLED=False), self._committed():
            response = self._press()
        self.assertEqual(detect_jobs(self.scan), [])
        messages = list(response.wsgi_request._messages)
        self.assertIn("YOLO_ENABLED", str(messages[0]))

    def test_no_committed_shard_set_is_refused(self):
        with self._committed(manifest=None, reason="no shard set"):
            response = self._press()
        self.assertEqual(detect_jobs(self.scan), [])
        messages = list(response.wsgi_request._messages)
        self.assertIn("no shard set", str(messages[0]))

    def test_a_second_press_while_a_run_is_open_is_refused(self):
        with self._committed():
            self._press()
            first = [row.pk for row in detect_jobs(self.scan)]
            response = self._press()

        self.assertEqual([row.pk for row in detect_jobs(self.scan)], first)
        messages = list(response.wsgi_request._messages)
        self.assertIn("already going", str(messages[-1]))

    def test_a_press_after_a_finished_run_reuses_it(self):
        # Idempotent rather than refused: a YOLO run is costly, so a
        # second press must not pay for shards already detected.
        with self._committed():
            self._press()
            rows = detect_jobs(self.scan)
            ExternalJob.objects.filter(pk__in=[row.pk for row in rows]).update(
                status=JobStatus.COMPLETED
            )
            with (
                patch("scanning.s3_sync.s3_active", return_value=True),
                patch("scanning.s3_sync.object_exists", return_value=True),
            ):
                response = self._press()

        self.assertEqual(len(detect_jobs(self.scan)), 3)
        self.assertEqual(
            {row.status for row in detect_jobs(self.scan)},
            {JobStatus.COMPLETED},
        )
        messages = list(response.wsgi_request._messages)
        self.assertIn("already detected", str(messages[-1]))

    def test_the_run_shows_on_the_process_page(self):
        # The stage writes no scan status, so the rows are the only
        # place a viewer can see it happening.
        with self._committed():
            self._press()
        self.client.force_login(self.staff)
        response = self.client.get(
            reverse("process_actions", kwargs={"pk": self.scan.pk})
        )
        self.assertIn("Detection running", response.json()["html"])
