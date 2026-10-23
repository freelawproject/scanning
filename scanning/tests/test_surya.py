"""Tests for the Surya stage (issue #364).

Surya is a RunPod engine, so the lifecycle it shares with dots.mocr --
the claim, every compare-and-swap, the retry ledger, the deadlines --
is covered in ``test_jobs.py`` and ``test_dots_mocr.py`` and not again
here. What is tested here is what makes Surya its own engine:

- the two switches, and the endpoint id that is blank until #320 lands
- a payload that carries no decode parameter, ever
- rows at ``EXTRACT``/``SURYA`` that do not collide with Mistral's
- a hole that is never carried, stable or not
- a wave counted against this engine's own cap
- the sweep that starts the read (#500), one run per shard set, and
  the bar that shows the run and offers no button

No HTTP and no S3: ``runpod_client`` and the S3 helpers are patched.
"""

from unittest.mock import patch

from django.template.loader import render_to_string
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from scanning import jobs, mistral_ocr, runpod_client, surya
from scanning.factories import ScanFactory
from scanning.models import (
    ExternalJob,
    JobEngine,
    JobProvider,
    JobStage,
    JobStatus,
    Status,
)
from scanning.tests.test_jobs import make_manifest
from scanning.tests.test_views import ScanningTestCase

SURYA = {
    "RUNPOD_ENABLED": True,
    "RUNPOD_API_KEY": "key-1",
    "RUNPOD_PRESIGNED_TTL": 3600,
    "RUNPOD_REQUEST_TIMEOUT": 600,
    "SURYA_ENABLED": True,
    "RUNPOD_SURYA_ENDPOINT_ID": "ep-surya",
    "SURYA_MAX_CONCURRENCY": 4,
    "SURYA_MAX_ATTEMPTS": 3,
    "SURYA_SECONDS_PER_PAGE": 4.0,
    # The other engines and doctor off, so a tick exercises this wave
    # alone.
    "DOTS_MOCR_ENABLED": False,
    "YOLO_ENABLED": False,
    "DOCTOR_ENABLED": False,
    "MISTRAL_API_KEY": "",
}


def surya_jobs(scan):
    """Return a scan's Surya rows in shard order.

    :param scan: The scan to look up.
    :returns: Its rows.
    :rtype: list[ExternalJob]
    """
    return list(
        ExternalJob.objects.filter(
            scan=scan,
            stage=JobStage.EXTRACT,
            engine=JobEngine.SURYA,
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
    """Both the stage switch and the endpoint id are required."""

    @override_settings(**SURYA)
    def test_on_with_everything_set(self):
        self.assertTrue(surya.enabled())

    @override_settings(**{**SURYA, "SURYA_ENABLED": False})
    def test_off_without_the_stage_switch(self):
        self.assertFalse(surya.enabled())

    @override_settings(**{**SURYA, "RUNPOD_SURYA_ENDPOINT_ID": ""})
    def test_off_without_an_endpoint(self):
        # Every environment holds a blank one until the endpoint of
        # #320 exists, so this is the normal state on the day of the
        # merge.
        self.assertFalse(surya.enabled())

    @override_settings(**{**SURYA, "RUNPOD_ENABLED": False})
    def test_off_without_the_account_switch(self):
        self.assertFalse(surya.enabled())

    @override_settings(**{**SURYA, "DOTS_MOCR_ENABLED": True})
    def test_one_engine_off_leaves_the_other_on(self):
        from scanning import dots_mocr

        with override_settings(RUNPOD_SURYA_ENDPOINT_ID=""):
            self.assertFalse(surya.enabled())
            with override_settings(RUNPOD_DOTSMOCR_ENDPOINT_ID="ep-dots"):
                self.assertTrue(dots_mocr.enabled())


# ── the payload ─────────────────────────────────────────────────────
@override_settings(**SURYA)
class TestBuildPayload(ScanningTestCase):
    """What the worker is asked to do, and what it is never asked."""

    def _row(self, **fields):
        scan = ScanFactory()
        (row,) = surya.ensure_extract_jobs(
            scan, make_manifest(shard_count=1, pages_per_shard=10)
        )
        if fields:
            ExternalJob.objects.filter(pk=row.pk).update(**fields)
            row.refresh_from_db()
        return row

    def test_the_payload_names_the_action_the_urls_and_the_render(self):
        row = self._row(result_key="jobs/extract/surya/r1-s0-a1.json")
        payload = surya.build_payload(row, "https://s3/in", "https://s3/out")

        self.assertEqual(payload["action"], "ocr")
        self.assertEqual(payload["scan_pk"], row.scan_id)
        self.assertEqual(payload["pdf_url"], "https://s3/in")
        self.assertEqual(payload["result_url"], "https://s3/out")
        self.assertEqual(
            payload["result_key"], "jobs/extract/surya/r1-s0-a1.json"
        )
        self.assertEqual(payload["dpi"], 200)
        self.assertEqual(payload["num_threads"], 16)

    def test_the_payload_carries_no_decode_parameter(self):
        # The worker answers BAD_INPUT to any of these (#320): what the
        # model sees is what the kit measured.
        payload = surya.build_payload(self._row(), "in", "out")
        for name in (
            "temperature",
            "top_p",
            "max_tokens",
            "max_completion_tokens",
        ):
            self.assertNotIn(name, payload)

    def test_a_row_override_reaches_the_payload(self):
        # An experiment writes the knob on the row rather than on a
        # deploy.
        row = self._row()
        row.input_manifest = {**row.input_manifest, "dpi": 400}
        payload = surya.build_payload(row, "in", "out")
        self.assertEqual(payload["dpi"], 400)

    def test_the_render_matches_the_other_stages(self):
        # Every bbox of the ensemble lives in one pixel space.
        self.assertEqual(surya.DPI, 200)


# ── the rows ────────────────────────────────────────────────────────
@override_settings(**SURYA)
class TestEnsureExtractJobs(ScanningTestCase):
    """One row per original shard, and never a second run for free."""

    def test_one_row_per_shard_over_the_original(self):
        scan = ScanFactory()
        rows = surya.ensure_extract_jobs(
            scan, make_manifest(shard_count=3, pages_per_shard=10)
        )

        self.assertEqual(len(rows), 3)
        for index, job in enumerate(rows):
            self.assertEqual(job.stage, JobStage.EXTRACT)
            self.assertEqual(job.engine, JobEngine.SURYA)
            self.assertEqual(job.provider, JobProvider.RUNPOD)
            self.assertEqual(job.status, JobStatus.PENDING)
            self.assertIsNone(job.opinion)
            self.assertIsNone(job.apply_run)
            self.assertEqual(job.shard_index, index)
            self.assertEqual(job.input_manifest["from_page"], index * 10)
            self.assertIn("shards/", job.input_key)
            self.assertNotIn("bitonal", job.input_key)

    def test_a_second_call_reuses_the_run(self):
        scan = ScanFactory()
        manifest = make_manifest(shard_count=2)
        first = surya.ensure_extract_jobs(scan, manifest)
        second = surya.ensure_extract_jobs(scan, manifest)
        self.assertEqual([r.pk for r in first], [r.pk for r in second])

    def test_the_mistral_rows_of_the_stage_do_not_collide(self):
        # ``engine`` is part of the unique key, so two engines read the
        # same shard set at the same time.
        scan = ScanFactory()
        manifest = make_manifest(shard_count=2)
        surya.ensure_extract_jobs(scan, manifest)
        mistral_ocr.ensure_extract_jobs(scan, manifest)

        self.assertEqual(len(surya_jobs(scan)), 2)
        self.assertEqual(
            ExternalJob.objects.filter(
                scan=scan, stage=JobStage.EXTRACT
            ).count(),
            4,
        )

    def test_an_unchanged_shard_is_carried_into_a_replacement_run(self):
        # The results are kept for the glue, so a run replaced for one
        # dead shard re-pays that shard alone.
        scan = ScanFactory()
        manifest = make_manifest(shard_count=2, pages_per_shard=10)
        rows = surya.ensure_extract_jobs(scan, manifest)
        ExternalJob.objects.filter(pk=rows[0].pk).update(
            status=JobStatus.COMPLETED,
            result_key="jobs/extract/surya/r1-s0-a1.json",
            completed_at=timezone.now(),
        )
        ExternalJob.objects.filter(pk=rows[1].pk).update(
            status=JobStatus.FAILED
        )

        with (
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch("scanning.s3_sync.object_exists", return_value=True),
        ):
            fresh = surya.ensure_extract_jobs(scan, manifest)

        self.assertEqual(fresh[0].run, 2)
        self.assertEqual(fresh[0].status, JobStatus.COMPLETED)
        self.assertEqual(
            fresh[0].result_key, "jobs/extract/surya/r1-s0-a1.json"
        )
        self.assertEqual(fresh[1].status, JobStatus.PENDING)

    def test_a_stable_hole_is_not_carried(self):
        # The stable-hole rule of #238 trusts a deterministic worker.
        # surya's client reads a looped answer again at a temperature it
        # raises itself, so two runs with the same hole are two unlucky
        # runs, not an answer.
        scan = ScanFactory()
        manifest = make_manifest(shard_count=1, pages_per_shard=2)
        for run in (1, 2):
            rows = surya.ensure_extract_jobs(
                scan, manifest, force_new_run=run == 2
            )
            ExternalJob.objects.filter(pk=rows[0].pk).update(
                status=JobStatus.COMPLETED,
                result_key=f"jobs/extract/surya/r{run}-s0-a1.json",
                provider_meta={"output": {"failed_pages": [1]}},
            )
        row = surya_jobs(scan)[-1]
        self.assertTrue(jobs.hole_is_stable(row))

        with (
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch("scanning.s3_sync.object_exists", return_value=True),
        ):
            fresh = surya.ensure_extract_jobs(
                scan, manifest, force_new_run=True
            )
        self.assertEqual(fresh[0].run, 3)
        self.assertEqual(fresh[0].status, JobStatus.PENDING)


# ── the wave and the poll ───────────────────────────────────────────
@override_settings(**SURYA)
class TestTheWave(ScanningTestCase):
    """The shared RunPod wave serves Surya with no new code."""

    def setUp(self):
        super().setUp()
        self.scan = ScanFactory()
        self.jobs = surya.ensure_extract_jobs(
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

    def _tick(self, **kwargs):
        with patch(
            "scanning.runpod_client.submit_job", return_value="job-1"
        ) as submit:
            summary = jobs.submit_pending(**kwargs)
        return summary, submit

    def test_a_wave_sends_every_row_to_the_surya_endpoint(self):
        with patch(
            "scanning.runpod_client.endpoint_config",
            return_value=("https://api/ep-surya", {}),
        ) as config:
            summary, submit = self._tick()

        self.assertEqual(summary.submitted, 3)
        self.assertEqual(submit.call_count, 3)
        self.assertEqual(config.call_args.args[0], "ep-surya")
        for job in surya_jobs(self.scan):
            self.assertEqual(job.status, JobStatus.SUBMITTED)
            self.assertTrue(job.result_key.endswith(".json"))
            self.assertIn("jobs/extract/surya/", job.result_key)

    def test_the_engine_counts_against_its_own_cap(self):
        with override_settings(SURYA_MAX_CONCURRENCY=2):
            summary, submit = self._tick()
        self.assertEqual(summary.submitted, 2)
        self.assertEqual(submit.call_count, 2)

    def test_the_engine_switched_off_sends_nothing(self):
        with override_settings(SURYA_ENABLED=False):
            summary, submit = self._tick()
        submit.assert_not_called()
        self.assertEqual(summary.submitted, 0)

    def test_a_poll_completes_the_row_and_keeps_the_summary(self):
        submitted = timezone.now()
        row = self.jobs[0]
        ExternalJob.objects.filter(pk=row.pk).update(
            status=JobStatus.SUBMITTED,
            external_id="job-1",
            result_key="jobs/extract/surya/r1-s0-a1.json",
            submitted_at=submitted,
            deadline=jobs.queue_deadline(submitted),
        )
        ExternalJob.objects.filter(
            pk__in=[r.pk for r in self.jobs[1:]]
        ).update(status=JobStatus.CANCELLED)

        with patch(
            "scanning.runpod_client.poll_once",
            return_value=outcome(
                JobStatus.COMPLETED,
                output={
                    "page_count": 10,
                    "failed_pages": [],
                    "empty_pages": [3],
                    "fallback_pages": [],
                    "dropped_block_pages": [],
                },
            ),
        ):
            summary = jobs.sweep_jobs()

        row.refresh_from_db()
        self.assertEqual(summary.completed, 1)
        self.assertEqual(row.status, JobStatus.COMPLETED)
        self.assertEqual(row.provider_meta["output"]["empty_pages"], [3])

    def test_an_empty_page_is_not_a_hole(self):
        # ``has_unread_pages`` reads the two names the page-number
        # reader cares about. An empty page is the glue's question, not
        # the carry's (#364).
        row = self.jobs[0]
        ExternalJob.objects.filter(pk=row.pk).update(
            status=JobStatus.COMPLETED,
            provider_meta={"output": {"empty_pages": [3], "failed_pages": []}},
        )
        row.refresh_from_db()
        self.assertFalse(jobs.has_unread_pages(row))


# ── the sweep that starts the read (#500) ──────────────────────────
@override_settings(**SURYA)
class TestEnqueueMissingRuns(ScanningTestCase):
    """The daemon starts the Surya read (#500), under the rule of the
    detection and dots.mocr sweeps: one run per shard set, ever. It
    replaced the staff button of #364.
    """

    FINGERPRINT = "2048:20"

    def setUp(self):
        super().setUp()
        self.manifest = make_manifest(shard_count=2, pages_per_shard=10)
        surya._REFUSED.clear()
        self.addCleanup(surya._REFUSED.clear)

    def _scan(
        self,
        status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
        fingerprint=None,
    ):
        return ScanFactory(
            page_count=20,
            status=status,
            source_fingerprint=(
                self.FINGERPRINT if fingerprint is None else fingerprint
            ),
        )

    def _sweep(self, s3=True, manifest=True, **overrides):
        with (
            override_settings(**overrides),
            patch("scanning.s3_sync.s3_active", return_value=s3),
            patch(
                "scanning.sharding.committed_manifest",
                return_value=(
                    (self.manifest, "") if manifest else (None, "refused")
                ),
            ) as committed,
            patch("scanning.runpod_client.submit_job") as submit,
        ):
            started = surya.enqueue_missing_runs()
        self.committed = committed
        self.submit = submit
        return started

    def test_a_volume_with_no_read_gets_one_run(self):
        scan = self._scan()
        with self.assertLogs("scanning.surya", level="INFO") as logs:
            self.assertEqual(self._sweep(), 1)
        rows = surya_jobs(scan)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r.status for r in rows}, {JobStatus.PENDING})
        self.assertEqual({r.provider for r in rows}, {JobProvider.RUNPOD})
        self.assertEqual(
            {r.source_fingerprint for r in rows}, {self.FINGERPRINT}
        )
        self.assertIn("Started Surya OCR run 1", "".join(logs.output))

    def test_the_sweep_calls_no_runpod_endpoint(self):
        # Rows only: the wave claims, signs and submits.
        self._scan()
        self._sweep()
        self.submit.assert_not_called()

    def test_the_sweep_never_cuts_shards(self):
        self._scan()
        with patch("scanning.sharding.ensure_shards") as cut:
            self._sweep()
        cut.assert_not_called()

    def test_a_shard_set_is_read_once(self):
        scan = self._scan()
        self.assertEqual(self._sweep(), 1)
        first = [r.pk for r in surya_jobs(scan)]
        self.assertEqual(self._sweep(), 0)
        self.assertEqual([r.pk for r in surya_jobs(scan)], first)
        self.committed.assert_not_called()

    def test_a_dead_run_is_not_re_run_by_a_tick(self):
        # Every attempt is paid: a dead run waits for a person
        # (``enqueue_surya_ocr``).
        scan = self._scan()
        self._sweep()
        ExternalJob.objects.filter(scan=scan).update(status=JobStatus.FAILED)
        self.assertEqual(self._sweep(), 0)
        self.assertEqual(
            {r.status for r in surya_jobs(scan)}, {JobStatus.FAILED}
        )

    def test_a_re_cut_shard_set_gets_a_run(self):
        scan = self._scan()
        self._sweep()
        ExternalJob.objects.filter(scan=scan).update(status=JobStatus.CONSUMED)
        scan.source_fingerprint = "4096:20"
        scan.save(update_fields=["source_fingerprint"])
        self.manifest = make_manifest(shard_count=4, pages_per_shard=5)
        with patch("scanning.s3_sync.object_exists", return_value=False):
            self.assertEqual(self._sweep(), 1)
        live = surya.live_extract_jobs(scan)
        self.assertEqual(len(live), 4)
        self.assertEqual({r.source_fingerprint for r in live}, {"4096:20"})

    def test_an_apply_run_row_answers_for_no_shard_set(self):
        # The one-page rows of a corrected volume are not a volume run.
        from scanning.models import ApplyRun

        scan = self._scan()
        run = ApplyRun.objects.create(scan=scan, number=1)
        jobs.ensure_shard_jobs(
            scan,
            make_manifest(shard_count=1, pages_per_shard=1),
            stage=JobStage.EXTRACT,
            engine=JobEngine.SURYA,
            provider=JobProvider.RUNPOD,
            reuse_results=True,
            apply_run=run,
        )
        self.assertEqual(self._sweep(), 1)
        self.assertEqual(len(surya.live_extract_jobs(scan)), 2)

    def test_a_mistral_run_answers_for_no_surya_run(self):
        # The same stage, another engine: ``engine`` is in the key.
        scan = self._scan()
        mistral_ocr.ensure_extract_jobs(scan, self.manifest)
        self.assertEqual(self._sweep(), 1)
        self.assertEqual(len(surya_jobs(scan)), 2)

    def test_a_volume_without_a_shard_set_is_not_swept(self):
        self._scan(fingerprint="")
        self.assertEqual(self._sweep(), 0)
        self.committed.assert_not_called()

    def test_a_status_outside_the_sweep_is_left_alone(self):
        # The detection sweep's rule (#250): the pipeline owns a QUEUED
        # or PROCESSING scan and may re-cut its set; ERROR and every
        # status past review 2 come back through the re-queue. An
        # approved volume's opinions are glued, and no pass glues them
        # again for a late read.
        for status in (
            Status.QUEUED,
            Status.PROCESSING,
            Status.REDACTION_REVIEW_DONE,
            Status.ERROR,
            Status.APPROVED,
        ):
            with self.subTest(status=status):
                scan = self._scan(status=status)
                self.assertEqual(self._sweep(), 0)
                self.assertEqual(surya_jobs(scan), [])

    def test_every_sweep_status_is_swept(self):
        # From the upload to the end of review 2, the detection sweep's
        # set: the read is over the original shards, which exist from
        # the moment the pipeline cut them, and a page deleted in
        # review 1 is in that set either way.
        from scanning import yolo

        for status in sorted(yolo.SWEEP_STATUSES):
            with self.subTest(status=status):
                scan = self._scan(status=status)
                self.assertEqual(self._sweep(), 1)
                self.assertEqual(len(surya_jobs(scan)), 2)

    def test_no_endpoint_no_sweep(self):
        scan = self._scan()
        self.assertEqual(self._sweep(RUNPOD_SURYA_ENDPOINT_ID=""), 0)
        self.committed.assert_not_called()
        self.assertEqual(surya_jobs(scan), [])

    def test_the_stage_switch_off_no_sweep(self):
        scan = self._scan()
        self.assertEqual(self._sweep(SURYA_ENABLED=False), 0)
        self.committed.assert_not_called()
        self.assertEqual(surya_jobs(scan), [])

    def test_no_s3_no_sweep(self):
        scan = self._scan()
        self.assertEqual(self._sweep(s3=False), 0)
        self.committed.assert_not_called()
        self.assertEqual(surya_jobs(scan), [])

    def test_a_few_volumes_per_tick_newest_first(self):
        scans = [self._scan() for _ in range(3)]
        self.assertEqual(self._sweep(SURYA_MAX_CONCURRENCY=2), 2)
        self.assertEqual(
            [scan.pk for scan in scans if surya_jobs(scan)],
            [scans[1].pk, scans[2].pk],
        )
        self.assertEqual(self._sweep(SURYA_MAX_CONCURRENCY=2), 1)
        self.assertTrue(all(len(surya_jobs(scan)) == 2 for scan in scans))

    def test_a_refused_shard_set_is_looked_at_once(self):
        scan = self._scan()
        with self.assertLogs("scanning.surya", level="INFO") as logs:
            self.assertEqual(self._sweep(manifest=False), 0)
        self.assertIn("refused", "".join(logs.output))
        self.assertEqual(self._sweep(manifest=False), 0)
        self.committed.assert_not_called()
        self.assertEqual(surya_jobs(scan), [])

    def test_the_sweep_writes_no_scan_status(self):
        scan = self._scan(status=Status.READY_FOR_REDACTION_REVIEW)
        self._sweep()
        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.READY_FOR_REDACTION_REVIEW)


# ── the step-1 bar (#500) ───────────────────────────────────────────
@override_settings(**SURYA)
class TestProcessBar(ScanningTestCase):
    """The bar shows the Surya run and offers no button: the daemon
    starts the read (#500)."""

    def setUp(self):
        super().setUp()
        self.staff = self.make_staff_user()
        self.scan = ScanFactory(
            page_count=20, status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )
        self.url = reverse("process_actions", kwargs={"pk": self.scan.pk})

    def _bar(self, user=None):
        self.client.force_login(user or self.staff)
        return self.client.get(f"{self.url}?step=1").json()["html"]

    def test_the_run_shows_on_the_bar(self):
        surya.ensure_extract_jobs(
            self.scan, make_manifest(shard_count=2, pages_per_shard=10)
        )
        self.assertIn("Surya OCR running", self._bar())

    def test_staff_keeps_the_files_link(self):
        surya.ensure_extract_jobs(
            self.scan, make_manifest(shard_count=2, pages_per_shard=10)
        )
        self.assertIn(
            reverse(
                "glued_output_index",
                kwargs={"pk": self.scan.pk, "output": "surya"},
            ),
            self._bar(),
        )

    def test_nobody_is_offered_a_surya_button(self):
        for user in (self.staff, self.make_user()):
            with self.subTest(staff=user.is_staff):
                html = self._bar(user)
                self.assertNotIn("Run Surya OCR", html)
                self.assertNotIn("Re-run Surya OCR", html)
                self.assertNotIn("start-surya", html)

    def test_the_mistral_button_stays(self):
        with override_settings(MISTRAL_API_KEY="key"):
            self.assertIn("Run Mistral OCR", self._bar())

    def test_the_route_is_gone(self):
        from django.urls import NoReverseMatch

        with self.assertRaises(NoReverseMatch):
            reverse("start_surya_ocr", kwargs={"pk": self.scan.pk})


# ── the files index ─────────────────────────────────────────────────
@override_settings(**SURYA)
class TestTheFilesIndex(ScanningTestCase):
    """The triage tool of a read nothing glues yet (#243)."""

    def setUp(self):
        super().setUp()
        self.staff = self.make_staff_user()
        self.scan = ScanFactory()
        self.rows = surya.ensure_extract_jobs(
            self.scan, make_manifest(shard_count=2, pages_per_shard=10)
        )
        self.client.force_login(self.staff)

    def _index(self):
        return self.client.get(
            reverse(
                "glued_output_index",
                kwargs={"pk": self.scan.pk, "output": "surya"},
            )
        ).json()

    def test_the_index_lists_the_run_and_its_shards(self):
        data = self._index()
        self.assertEqual(data["engine"], JobEngine.SURYA)
        self.assertEqual(data["stage"], JobStage.EXTRACT)
        self.assertEqual(data["live_run"], 1)
        self.assertEqual(len(data["runs"][0]["shards"]), 2)
        self.assertFalse(data["runs"][0]["glued"])

    def test_the_index_reads_the_list_names_off_the_glue(self):
        """One table of names (``surya.PAGE_LISTS``, #368), so a fifth
        list reaches the index with the glue that reports it."""
        from scanning import views_process

        self.assertEqual(
            views_process.SHARD_PAGE_LISTS[JobEngine.SURYA],
            tuple(name for name, _member in surya.PAGE_LISTS),
        )

    def test_a_shard_carries_the_workers_own_page_lists(self):
        ExternalJob.objects.filter(pk=self.rows[0].pk).update(
            status=JobStatus.COMPLETED,
            result_key="jobs/extract/surya/r1-s0-a1.json",
            provider_meta={
                "output": {
                    "failed_pages": [2],
                    "empty_pages": [3],
                    "fallback_pages": [4],
                    "dropped_block_pages": [5],
                }
            },
        )
        shard = self._index()["runs"][0]["shards"][0]
        self.assertEqual(shard["failed_pages"], [2])
        self.assertEqual(shard["empty_pages"], [3])
        self.assertEqual(shard["fallback_pages"], [4])
        self.assertEqual(shard["dropped_block_pages"], [5])
        # dots.mocr's own names are not reported by this worker, and an
        # empty list would read as "none" where the truth is "not a
        # question here".
        self.assertNotIn("filtered_pages", shard)
        self.assertNotIn("repaired_pages", shard)

    def test_the_volume_route_says_the_run_is_not_glued(self):
        # The rows of this fixture are not all COMPLETED, so the pass
        # of #368 glued nothing and the object is not in the bucket.
        with (
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch("scanning.s3_sync.object_exists", return_value=False),
        ):
            response = self.client.get(
                reverse(
                    "serve_glued_volume",
                    kwargs={"pk": self.scan.pk, "output": "surya", "run": 1},
                )
            )
        self.assertEqual(response.status_code, 404)
        self.assertIn("not glued yet", response.json()["error"])


# ── the shared control ──────────────────────────────────────────────
class TestTheEngineControl(ScanningTestCase):
    """The template both OCR controls render from (#364). The Surya
    control passes no ``start_url`` since #500, so the button under
    test is the Mistral one."""

    def test_the_confirm_text_is_escaped_for_javascript(self):
        # The text lands inside a JavaScript string. HTML escaping is
        # not enough: the browser decodes the entity before the
        # JavaScript parser reads the handler, so an apostrophe would
        # break it and the click would POST the paid work with no
        # question.
        html = render_to_string(
            "scanning/_ocr_engine_actions.html",
            {
                "scan": ScanFactory(),
                "user": self.make_staff_user(),
                "run": None,
                "label": "Mistral OCR",
                "start_url": "start_mistral_ocr",
                "files_slug": "mistral",
                "title": "t",
                "confirm": "Read Mistral's pages?",
            },
        )
        self.assertIn(r"confirm('Read Mistral\u0027s pages?')", html)
        self.assertNotIn("Mistral's", html)
        self.assertNotIn("Mistral&#x27;s", html)
