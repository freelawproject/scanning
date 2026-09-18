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
- the button that is the only thing which creates a row

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


# ── the button ──────────────────────────────────────────────────────
@override_settings(**SURYA)
class TestStartSuryaOcr(ScanningTestCase):
    """The only thing that creates a Surya row."""

    def setUp(self):
        super().setUp()
        self.staff = self.make_staff_user()
        self.scan = ScanFactory(
            page_count=20, status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )
        self.url = reverse("start_surya_ocr", kwargs={"pk": self.scan.pk})
        self.manifest = make_manifest(shard_count=2, pages_per_shard=10)

    _UNSET = object()

    def _committed(self, manifest=_UNSET, reason=""):
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

    def _messages(self, response):
        return [str(m) for m in response.wsgi_request._messages]

    def test_staff_press_creates_one_row_per_shard(self):
        with self._committed():
            response = self._press()
        self.assertRedirects(
            response,
            reverse("scan_process", kwargs={"pk": self.scan.pk}),
            fetch_redirect_response=False,
        )
        self.assertEqual(len(surya_jobs(self.scan)), 2)
        self.assertIn("Queued Surya OCR", self._messages(response)[0])

    def test_the_request_never_calls_runpod(self):
        with (
            self._committed(),
            patch("scanning.runpod_client.submit_job") as submit,
        ):
            self._press()
        submit.assert_not_called()

    def test_the_request_never_cuts_shards(self):
        with (
            self._committed(),
            patch("scanning.sharding.ensure_shards") as cut,
        ):
            self._press()
        cut.assert_not_called()

    def test_a_non_staff_user_is_refused(self):
        with self._committed():
            response = self._press(self.make_user())
        self.assertEqual(surya_jobs(self.scan), [])
        self.assertIn("Only staff", self._messages(response)[0])

    def test_an_anonymous_user_is_redirected_to_login(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

    def test_a_get_is_rejected(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_no_endpoint_is_refused(self):
        with (
            override_settings(RUNPOD_SURYA_ENDPOINT_ID=""),
            self._committed(),
        ):
            response = self._press()
        self.assertEqual(surya_jobs(self.scan), [])
        self.assertIn("RUNPOD_SURYA_ENDPOINT_ID", self._messages(response)[0])

    def test_a_manifest_with_no_shard_is_refused(self):
        # ``ensure_extract_jobs`` then creates no row, and the "already
        # read" line would address ``created[0]`` and raise.
        empty = {**self.manifest, "shards": []}
        with self._committed(manifest=empty):
            response = self._press()
        self.assertEqual(surya_jobs(self.scan), [])
        self.assertIn("no part to read", self._messages(response)[-1])

    def test_no_committed_shard_set_is_refused(self):
        with self._committed(manifest=None, reason="no shard set"):
            response = self._press()
        self.assertEqual(surya_jobs(self.scan), [])
        self.assertIn("no shard set", self._messages(response)[0])

    def test_a_second_press_while_a_run_is_open_is_refused(self):
        with self._committed():
            self._press()
            first = [r.pk for r in surya_jobs(self.scan)]
            response = self._press()
        self.assertEqual([r.pk for r in surya_jobs(self.scan)], first)
        self.assertIn("already going", self._messages(response)[-1])

    def test_a_press_after_a_finished_run_reuses_it(self):
        with self._committed():
            self._press()
            rows = surya_jobs(self.scan)
            ExternalJob.objects.filter(pk__in=[r.pk for r in rows]).update(
                status=JobStatus.COMPLETED,
                result_key="jobs/extract/surya/r1-s0-a1.json",
            )
            with (
                patch("scanning.s3_sync.s3_active", return_value=True),
                patch("scanning.s3_sync.object_exists", return_value=True),
            ):
                response = self._press()
        self.assertEqual(len(surya_jobs(self.scan)), 2)
        self.assertIn("already read", self._messages(response)[-1])

    def test_the_bar_offers_the_button_to_staff(self):
        # Beside the Mistral control on the step-1 bar, because both
        # read the original shards.
        url = reverse("process_actions", kwargs={"pk": self.scan.pk})
        self.client.force_login(self.staff)
        self.assertIn(
            "Run Surya OCR",
            self.client.get(f"{url}?step=1").json()["html"],
        )

    def test_the_bar_offers_a_curator_no_button(self):
        url = reverse("process_actions", kwargs={"pk": self.scan.pk})
        self.client.force_login(self.make_user())
        self.assertNotIn(
            "Run Surya OCR",
            self.client.get(f"{url}?step=1").json()["html"],
        )

    def test_the_run_shows_on_the_process_page(self):
        with self._committed():
            self._press()
        self.client.force_login(self.staff)
        response = self.client.get(
            reverse("process_actions", kwargs={"pk": self.scan.pk})
        )
        self.assertIn("Surya OCR running", response.json()["html"])


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
        # There is no glue yet (#364), so this is the true answer.
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
    """The template both OCR buttons render from (#364)."""

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
                "label": "Surya OCR",
                "start_url": "start_surya_ocr",
                "files_slug": "surya",
                "title": "t",
                "confirm": "Read Mistral's pages?",
            },
        )
        self.assertIn(r"confirm('Read Mistral\u0027s pages?')", html)
        self.assertNotIn("Mistral's", html)
        self.assertNotIn("Mistral&#x27;s", html)
