"""The scan's caches stay on disk where nothing reads them (#582).

``Scan.LARGE_FIELDS`` hold kilobytes to megabytes a row, and a query
that joins the scan repeats them on every row it returns. These tests
run the pages and the tick passes that defer them and read the SQL:
a query that names a cache column is a regression, whether it is the
query itself or a reader that loads the deferred field row by row.
The column list is in the SQL whether or not a row comes back, so each
test also asserts that the scan was read at all, and never passes for
a query that did not run.

The opinions list, the job sweeps and the submit wave keep their tests
beside the other tests of their areas.

The progress poll, the queue pages, the admin lists and ``live_run``
follow (#586).
"""

from unittest.mock import patch

from django.contrib import admin
from django.core.exceptions import FieldDoesNotExist
from django.db import connection
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from scanning.admin import ScanCachesDeferred
from scanning.factories import (
    ExternalJobFactory,
    OpinionFactory,
    OpinionScanFactory,
    ScanFactory,
    UserFactory,
    VolumeFactory,
)
from scanning.models import (
    JobEngine,
    JobProvider,
    JobStage,
    JobStatus,
    OpinionReviewStatus,
    PageRepairRequest,
    Scan,
    Status,
)
from scanning.tests.test_views import ScanningTestCase, large_scan_columns


class _CacheColumns(ScanningTestCase):
    def assert_caches_stay_on_disk(self, run):
        """Run ``run`` and assert the scan was read without its caches.

        :param run: A callable of no arguments.
        :returns: What ``run`` returned.
        """
        with CaptureQueriesContext(connection) as captured:
            result = run()
        self.assertTrue(
            any('"scanning_scan"' in query["sql"] for query in captured),
            "no query read the scan, so the test proves nothing",
        )
        self.assertEqual(large_scan_columns(captured), [])
        return result

    def assert_deferred(self, queryset, prefix=""):
        """Assert a queryset defers every cache of the scan it reads."""
        names, deferring = queryset.query.deferred_loading
        self.assertTrue(deferring)
        self.assertLessEqual(set(Scan.large_fields(prefix)), set(names))


class TestPages(_CacheColumns):
    """The pages that read an opinion or a request with its scan."""

    def setUp(self):
        self.client.force_login(self.make_user())

    def test_the_review_page_of_an_opinion(self):
        opinion = OpinionFactory()

        response = self.assert_caches_stay_on_disk(
            lambda: self.client.get(
                reverse("opinion_review", kwargs={"pk": opinion.pk})
            )
        )

        self.assertEqual(response.status_code, 200)

    def test_the_legacy_opinion_list(self):
        scan = ScanFactory()
        for page in (1, 9):
            OpinionScanFactory(scan=scan, page_start=page)

        response = self.assert_caches_stay_on_disk(
            lambda: self.client.get(reverse("legacy_opinion_list"))
        )

        self.assertEqual(response.status_code, 200)

    def test_the_repair_queue(self):
        scan = ScanFactory()
        for page in (1, 2):
            PageRepairRequest.objects.create(
                scan=scan,
                requested_by=self.make_user(),
                action=PageRepairRequest.Action.REPLACE,
                pdf_page=page,
            )

        response = self.assert_caches_stay_on_disk(
            lambda: self.client.get(reverse("repair_queue"))
        )

        self.assertEqual(response.status_code, 200)


class TestQuerysets(_CacheColumns):
    """The querysets a pass hands on: its caller reads the rows."""

    def test_the_opinion_passes(self):
        from scanning import opinion_figures, opinion_ocr, opinion_pdf, repairs

        for name, queryset in (
            ("opinion_pdf.owed", opinion_pdf.owed()),
            ("opinion_figures.owed", opinion_figures.owed()),
            ("opinion_ocr.reglue_rows", opinion_ocr.reglue_rows()),
            ("repairs.queue", repairs.queue()),
        ):
            with self.subTest(name):
                self.assert_deferred(queryset, "scan__")


class TestTickPasses(_CacheColumns):
    """The passes that walk scans or opinions with their scan."""

    def test_the_promotion_of_the_opinions(self):
        from scanning import opinions

        self.assert_caches_stay_on_disk(opinions.promote_ready_opinions)

    def test_the_ensemble(self):
        from scanning import ensemble

        with patch("scanning.s3_sync.s3_active", return_value=True):
            self.assert_caches_stay_on_disk(ensemble.run_tick)

    def test_the_final_xml_export(self):
        from scanning import final_xml

        self.assert_caches_stay_on_disk(
            lambda: final_xml.export_rows(None, None)
        )

    def test_the_two_read_sweeps(self):
        from scanning import dots_mocr, yolo

        ScanFactory(status=Status.AWAITING, source_fingerprint="f1")
        for name, module in (("dots_mocr", dots_mocr), ("yolo", yolo)):
            with (
                self.subTest(name),
                patch.object(module, "enabled", return_value=True),
                patch("scanning.s3_sync.s3_active", return_value=True),
                # The scan has no shards: the sweep reads it and passes
                # it over, and starts no run.
                patch(
                    "scanning.sharding.committed_manifest",
                    return_value=(None, "no shards"),
                ),
                patch.dict(module._REFUSED, clear=True),
            ):
                started = self.assert_caches_stay_on_disk(
                    module.enqueue_missing_runs
                )
                self.assertEqual(started, 0)

    def _detected_scan(self):
        """A scan in review 1's done state whose volume run was read."""
        scan = ScanFactory(status=Status.PAGE_COMPLETENESS_REVIEW_DONE)
        ExternalJobFactory(scan=scan, status=JobStatus.CONSUMED)
        return scan

    def test_the_promotion_of_the_scans(self):
        from scanning import review_states

        self._detected_scan()

        self.assert_caches_stay_on_disk(review_states.promote_ready_scans)

    def test_the_detection_queue(self):
        from scanning import yolo

        self._detected_scan()

        with patch("scanning.s3_sync.s3_active", return_value=True):
            self.assert_caches_stay_on_disk(yolo.queue_ready_runs)

    def test_the_bitonal_merge(self):
        from scanning import bitonal

        scan = ScanFactory(status=Status.AWAITING)
        ExternalJobFactory(
            scan=scan,
            stage=JobStage.CONVERT,
            engine=JobEngine.BITONAL,
            provider=JobProvider.DOCTOR,
        )

        self.assert_caches_stay_on_disk(bitonal.finish_ready_scans)

    def test_the_cancel_of_a_scans_open_jobs(self):
        from scanning import jobs

        scan = ScanFactory()
        # In flight: the cancel at the provider reads the row again.
        ExternalJobFactory(
            scan=scan,
            stage=JobStage.CONVERT,
            engine=JobEngine.BITONAL,
            provider=JobProvider.DOCTOR,
            status=JobStatus.SUBMITTED,
        )

        cancelled = self.assert_caches_stay_on_disk(
            lambda: jobs.abandon_open(scan, "replaced", stage=JobStage.CONVERT)
        )

        self.assertEqual(cancelled, 1)


class TestProgressPoll(_CacheColumns):
    """The poll every open tab on a busy scan sends each second (#586)."""

    def setUp(self):
        self.client.force_login(self.make_user())
        self.scan = ScanFactory(
            status=Status.PROCESSING,
            ocr_results=[{"pdf_page": 1, "detected": "1"}],
            progress_log="x" * 1000,
        )
        self.url = reverse("progress_api", kwargs={"pk": self.scan.pk})

    def test_the_first_poll_sends_the_cache_and_its_size(self):
        data = self.client.get(self.url).json()

        self.assertEqual(data["ocr_results"], self.scan.ocr_results)
        self.assertIsInstance(data["ocr_size"], int)
        self.assertNotIn("log", data)

    def test_a_poll_with_the_current_size_reads_no_cache(self):
        size = self.client.get(self.url).json()["ocr_size"]

        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(self.url, {"ocr_size": size})

        self.assertNotIn("ocr_results", response.json())
        # The size names the column and reads its header alone.
        size_call = 'pg_column_size("scanning_scan"."ocr_results")'
        queries = [
            {"sql": query["sql"].replace(size_call, "")} for query in captured
        ]
        self.assertTrue(any(size_call in q["sql"] for q in captured))
        self.assertEqual(large_scan_columns(queries), [])

    def test_a_moved_cache_is_sent_again(self):
        size = self.client.get(self.url).json()["ocr_size"]
        moved = self.scan.ocr_results + [{"pdf_page": 2, "detected": "2"}]
        Scan.objects.filter(pk=self.scan.pk).update(ocr_results=moved)

        data = self.client.get(self.url, {"ocr_size": size}).json()

        self.assertEqual(data["ocr_results"], moved)
        self.assertNotEqual(data["ocr_size"], size)

    def test_the_poll_costs_the_same_with_more_runs(self):
        """The engine summaries read the live run alone, in one query."""
        size = self.client.get(self.url).json()["ocr_size"]

        def queries():
            with CaptureQueriesContext(connection) as captured:
                self.client.get(self.url, {"ocr_size": size})
            return len(captured)

        before = queries()
        for run in (1, 2, 3):
            for shard in (0, 1):
                ExternalJobFactory(
                    scan=self.scan,
                    run=run,
                    shard_index=shard,
                    shard_count=2,
                    status=JobStatus.CONSUMED,
                )

        self.assertEqual(queries(), before)

    def test_the_poll_reads_no_job_json(self):
        """The engine summaries leave the rows' JSON on disk (#612)."""
        ExternalJobFactory(scan=self.scan, status=JobStatus.COMPLETED)

        with CaptureQueriesContext(connection) as captured:
            data = self.client.get(self.url).json()

        self.assertEqual(data["yolo_run"]["done"], 1)
        job_queries = [
            query["sql"]
            for query in captured
            if 'FROM "scanning_externaljob"' in query["sql"]
        ]
        self.assertTrue(job_queries)
        for sql in job_queries:
            self.assertNotIn('"input_manifest"', sql)
            self.assertNotIn('"provider_meta"', sql)


class TestLiveRun(ScanningTestCase):
    """``jobs.live_run`` reads the latest run in the query (#586)."""

    def test_only_the_latest_runs_rows_in_shard_order(self):
        from scanning import jobs

        scan = ScanFactory()
        for run in (1, 2):
            for shard in (1, 0):
                ExternalJobFactory(
                    scan=scan, run=run, shard_index=shard, shard_count=2
                )
        # Another target's later run is not this target's.
        ExternalJobFactory(run=5)

        with CaptureQueriesContext(connection) as captured:
            rows = jobs.live_run(scan, JobStage.DETECT, JobEngine.BLACKLETTER)

        self.assertEqual(
            [(r.run, r.shard_index) for r in rows], [(2, 0), (2, 1)]
        )
        self.assertEqual(len(captured), 1)
        # The database picks the run: the old runs never leave it.
        self.assertIn('"run" = (SELECT', captured[0]["sql"])

    def test_the_named_fields_alone(self):
        """A caller that names its fields reads no other column (#612)."""
        from scanning import jobs

        scan = ScanFactory()
        ExternalJobFactory(scan=scan, error_code="x")

        with CaptureQueriesContext(connection) as captured:
            rows = jobs.live_run(
                scan,
                JobStage.DETECT,
                JobEngine.BLACKLETTER,
                fields=jobs.SUMMARY_FIELDS,
            )
            summary = jobs.run_summary(
                scan, JobStage.DETECT, JobEngine.BLACKLETTER
            )

        self.assertEqual([row.error_code for row in rows], ["x"])
        self.assertEqual(summary["total"], 1)
        # One query each: no deferred column was loaded row by row.
        self.assertEqual(len(captured), 2)
        for query in captured:
            self.assertNotIn('"provider_meta"', query["sql"])

    def test_a_target_with_no_run_has_no_rows(self):
        from scanning import jobs

        self.assertEqual(
            jobs.live_run(
                ScanFactory(), JobStage.DETECT, JobEngine.BLACKLETTER
            ),
            [],
        )


class TestQueuePages(_CacheColumns):
    """The queue lists the scans of each volume under it (#586)."""

    def setUp(self):
        self.client.force_login(self.make_user())
        self.volume = VolumeFactory()

    def _scan(self):
        return ScanFactory(
            reporter=self.volume.reporter,
            volume=self.volume.volume_number,
            volume_obj=self.volume,
            uploaded_by=UserFactory(),
        )

    def test_the_queue(self):
        self._scan()

        def get():
            with CaptureQueriesContext(connection) as captured:
                response = self.client.get(reverse("queue"))
            self.assertEqual(response.status_code, 200)
            return captured

        one = get()
        self.assertEqual(large_scan_columns(one), [])
        self.assertTrue(
            any('"scanning_scan"' in q["sql"] for q in one),
            "no query read the scan, so the test proves nothing",
        )
        self._scan()
        self._scan()

        # The uploader comes in the prefetch, never one query per scan.
        self.assertEqual(len(get()), len(one))

    def test_the_volume_page(self):
        self._scan()

        response = self.assert_caches_stay_on_disk(
            lambda: self.client.get(
                reverse(
                    "queue_detail",
                    kwargs={
                        "reporter_slug": self.volume.reporter.short_name,
                        "vol": self.volume.volume_number,
                    },
                )
            )
        )

        self.assertEqual(response.status_code, 200)


class TestAdminLists(_CacheColumns):
    """The admin lists that show their row's scan (#586)."""

    def setUp(self):
        self.client.force_login(UserFactory(is_staff=True, is_superuser=True))

    def _changelist(self, model):
        meta = model._meta
        return reverse(f"admin:{meta.app_label}_{meta.model_name}_changelist")

    def test_every_list_that_shows_the_scan(self):
        """A list opens and joins its scan without the caches.

        The SQL names its columns whether a row comes back or not, so an
        empty table proves the join; a model with no ``scan`` path is a
        500 here and not at import.
        """
        deferring = [
            model
            for model, model_admin in admin.site._registry.items()
            if isinstance(model_admin, ScanCachesDeferred)
        ]
        self.assertGreaterEqual(len(deferring), 18)
        for model in deferring:
            with self.subTest(model.__name__):
                response = self.assert_caches_stay_on_disk(
                    lambda: self.client.get(self._changelist(model))
                )
                self.assertEqual(response.status_code, 200)

    def test_every_fk_column_stays_joined(self):
        """A list of joins replaces Django's join of every FK column."""
        request = RequestFactory().get("/")
        for model, model_admin in admin.site._registry.items():
            if not isinstance(model_admin, ScanCachesDeferred):
                continue
            joined = model_admin.get_list_select_related(request)
            for name in model_admin.get_list_display(request):
                try:
                    field = model._meta.get_field(name)
                except FieldDoesNotExist:
                    continue
                if field.many_to_one or field.one_to_one:
                    with self.subTest(f"{model.__name__}.{name}"):
                        self.assertIn(name, joined)

    def test_the_legacy_opinion_list_costs_the_same_with_more_rows(self):
        """The reporter column is joined, never read row by row."""
        url = self._changelist(OpinionScanFactory._meta.model)
        OpinionScanFactory()

        def queries():
            with CaptureQueriesContext(connection) as captured:
                self.assertEqual(self.client.get(url).status_code, 200)
            return len(captured)

        one = queries()
        OpinionScanFactory()
        OpinionScanFactory()

        self.assertEqual(queries(), one)

    def test_the_scan_list(self):
        ScanFactory(ocr_results=[{"pdf_page": 1}])

        response = self.assert_caches_stay_on_disk(
            lambda: self.client.get(self._changelist(Scan))
        )

        self.assertEqual(response.status_code, 200)

    def test_a_job_row_with_its_scan(self):
        ExternalJobFactory()

        response = self.assert_caches_stay_on_disk(
            lambda: self.client.get(
                self._changelist(ExternalJobFactory._meta.model)
            )
        )

        self.assertEqual(response.status_code, 200)


class TestTaggerGlue(_CacheColumns):
    """The tagger's glue pass joins the opinion's scan (#586)."""

    def test_the_glue_pass(self):
        from scanning import tagger

        with patch("scanning.s3_sync.s3_active", return_value=True):
            self.assert_caches_stay_on_disk(tagger.finish_ready_runs)


PAGE_MAP_COLUMN = '"scanning_applyrun"."page_map"'


class TestApplyRunPageMap(ScanningTestCase):
    """The apply run's page map stays on disk where a check reads none (#588)."""

    def setUp(self):
        from scanning.models import ApplyRun

        self.scan = ScanFactory(status=Status.PAGE_COMPLETENESS_REVIEW_DONE)
        self.page_map = {"pages": [{"original": 1}], "final_page_count": 1}
        self.run = ApplyRun.objects.create(
            scan=self.scan, number=1, page_map=self.page_map
        )

    def assert_no_page_map(self, run):
        """Run ``run`` and assert it read the apply run without its map."""
        with CaptureQueriesContext(connection) as captured:
            result = run()
        self.assertTrue(
            any('"scanning_applyrun"' in q["sql"] for q in captured),
            "no query read the apply run, so the test proves nothing",
        )
        self.assertEqual(
            [q["sql"] for q in captured if PAGE_MAP_COLUMN in q["sql"]], []
        )
        return result

    def test_the_standing_and_the_latest_run(self):
        from scanning import apply

        for name in ("current_run", "latest_run"):
            with self.subTest(name):
                run = self.assert_no_page_map(
                    lambda: getattr(apply, name)(self.scan)
                )
                self.assertEqual(run.pk, self.run.pk)
                # A reader of the map still gets it, on access.
                self.assertEqual(run.page_map, self.page_map)

    def test_the_promotion_of_the_scans(self):
        from scanning import review_states

        ExternalJobFactory(scan=self.scan, status=JobStatus.CONSUMED)

        self.assert_no_page_map(review_states.promote_ready_scans)

    def test_the_dead_row_note(self):
        from django.utils import timezone

        from scanning import apply

        type(self.run).objects.filter(pk=self.run.pk).update(
            built_at=timezone.now()
        )
        ExternalJobFactory(
            scan=self.scan,
            apply_run=self.run,
            stage=JobStage.CONVERT,
            engine=JobEngine.BITONAL,
            provider=JobProvider.DOCTOR,
            status=JobStatus.FAILED,
        )

        noted = self.assert_no_page_map(apply._note_dead_rows)

        self.assertEqual(noted, 1)

    def test_the_apply_glue_precheck(self):
        from django.utils import timezone

        from scanning import jobs, mistral_ocr

        type(self.run).objects.filter(pk=self.run.pk).update(
            built_at=timezone.now()
        )
        ExternalJobFactory(
            scan=self.scan,
            stage=JobStage.EXTRACT,
            engine=JobEngine.MISTRAL_OCR,
            provider=JobProvider.MISTRAL,
            status=JobStatus.CONSUMED,
        )

        self.assert_no_page_map(
            lambda: list(
                jobs.ready_apply_runs(
                    mistral_ocr.APPLY_GLUE_TARGET,
                    mistral_ocr.live_extract_jobs,
                    mistral_ocr.GLUE_MAX_ATTEMPTS,
                )
            )
        )


class TestVolumeRunsIndex(ScanningTestCase):
    """The apply glue's pre-check has an index to read (#588).

    The plan itself is the planner's choice, and a tiny test table with
    the statistics another test left is no measure of it: the test pins
    the index and the two queries' fit to it instead.
    """

    def test_the_index_carries_the_status_for_the_volume_runs(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT indexdef FROM pg_indexes WHERE indexname = %s",
                ["idx_job_volume_runs"],
            )
            (definition,) = cursor.fetchone()

        self.assertIn("(stage, engine, provider, scan_id, run)", definition)
        self.assertIn("INCLUDE (status)", definition)
        self.assertIn("WHERE (apply_run_id IS NULL)", definition)

    def test_both_questions_fit_the_index(self):
        """Each filters the index's leading columns and its condition."""
        from scanning import jobs, mistral_ocr

        ExternalJobFactory(
            stage=JobStage.EXTRACT,
            engine=JobEngine.MISTRAL_OCR,
            provider=JobProvider.MISTRAL,
            status=JobStatus.CONSUMED,
        )
        with CaptureQueriesContext(connection) as captured:
            list(
                jobs.ready_apply_runs(
                    mistral_ocr.APPLY_GLUE_TARGET,
                    mistral_ocr.live_extract_jobs,
                    mistral_ocr.GLUE_MAX_ATTEMPTS,
                )
            )
        prechecks = [
            q["sql"]
            for q in captured
            if q["sql"].startswith('SELECT "scanning_externaljob"."scan_id"')
        ]

        self.assertEqual(len(prechecks), 2, prechecks)
        for sql in prechecks:
            with self.subTest(sql[:60]):
                for column in ("stage", "engine", "provider"):
                    self.assertIn(f'"scanning_externaljob"."{column}" =', sql)
                self.assertIn(
                    '"scanning_externaljob"."apply_run_id" IS NULL', sql
                )


class TestScanFetches(_CacheColumns):
    """The views that fetch one scan, and the daemon reads of #602."""

    def setUp(self):
        self.client.force_login(self.make_staff_user())

    def test_no_view_fetches_the_whole_scan(self):
        """Every one-scan fetch of a view names what it keeps."""
        import inspect
        import re

        from scanning import views, views_api, views_process

        for module in (views, views_api, views_process):
            with self.subTest(module.__name__):
                self.assertEqual(
                    re.findall(
                        r"get_object_or_404\(\s*Scan\s*,\s*pk=pk\s*\)",
                        inspect.getsource(module),
                    ),
                    [],
                )

    def test_a_keep_must_name_a_cache(self):
        with self.assertRaises(ValueError):
            Scan.without_caches("status")

    def test_the_review_and_the_preview_reads(self):
        scan = ScanFactory(status=Status.READY_FOR_REDACTION_REVIEW)
        for name in (
            "serve_detections",
            "serve_redactions",
            "review_findings",
            "serve_scan_pdf",
        ):
            with self.subTest(name):
                response = self.assert_caches_stay_on_disk(
                    lambda: self.client.get(
                        reverse(name, kwargs={"pk": scan.pk})
                    )
                )
                self.assertLess(response.status_code, 500)

    def test_the_opinion_urls(self):
        opinion = OpinionFactory()
        kwargs = {"pk": opinion.scan_id, "opinion_pk": opinion.pk}
        for name in ("opinion_pdf_url", "opinion_blocking_cards"):
            with self.subTest(name):
                response = self.assert_caches_stay_on_disk(
                    lambda: self.client.get(reverse(name, kwargs=kwargs))
                )
                self.assertLess(response.status_code, 500)

    def test_the_blocking_cards_read_the_scan_in_the_join(self):
        opinion = OpinionFactory()
        url = reverse(
            "opinion_blocking_cards",
            kwargs={"pk": opinion.scan_id, "opinion_pk": opinion.pk},
        )

        with CaptureQueriesContext(connection) as captured:
            self.client.get(url)

        self.assertEqual(
            [q["sql"] for q in captured if 'FROM "scanning_scan"' in q["sql"]],
            [],
        )

    def test_the_blocking_cards_of_another_scan_are_not_found(self):
        opinion = OpinionFactory()
        other = ScanFactory()

        response = self.client.get(
            reverse(
                "opinion_blocking_cards",
                kwargs={"pk": other.pk, "opinion_pk": opinion.pk},
            )
        )

        self.assertEqual(response.status_code, 404)

    def test_the_claim(self):
        from scanning.management.commands import process_next_scan

        scan = ScanFactory(status=Status.QUEUED)

        claimed = self.assert_caches_stay_on_disk(process_next_scan.claim_next)

        self.assertEqual(claimed[0].pk, scan.pk)

    def test_the_stale_sweep(self):
        from datetime import timedelta

        from django.utils import timezone

        from scanning.management.commands import process_next_scan

        scan = ScanFactory(status=Status.PROCESSING)
        Scan.objects.filter(pk=scan.pk).update(
            processed_at=timezone.now() - timedelta(days=1)
        )

        self.assert_caches_stay_on_disk(process_next_scan.recover_stale)

    def test_the_volume_glue_candidates(self):
        from scanning import jobs

        ExternalJobFactory(status=JobStatus.COMPLETED)

        def walk():
            return list(
                jobs.ready_volume_runs(
                    JobStage.DETECT,
                    JobEngine.BLACKLETTER,
                    JobProvider.RUNPOD,
                    lambda scan: jobs.live_run(
                        scan, JobStage.DETECT, JobEngine.BLACKLETTER
                    ),
                    lambda rows: 0,
                    3,
                )
            )

        self.assertEqual(len(self.assert_caches_stay_on_disk(walk)), 1)


class TestApplyPrefilter(ScanningTestCase):
    """The page-number apply judges only a run that owes it (#602)."""

    def _scan(self, apply_state=None, status=JobStatus.CONSUMED):
        scan = ScanFactory(status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW)
        ExternalJobFactory(
            scan=scan,
            stage=JobStage.ANALYZE,
            engine=JobEngine.DOTS_MOCR,
            status=status,
            provider_meta={"apply": apply_state} if apply_state else {},
        )
        return scan

    def _run(self):
        from scanning import dots_mocr

        with (
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch(
                "scanning.services.run_compute_issues", return_value=True
            ) as compute,
        ):
            dots_mocr.apply_ready_runs()
        return {call.args[0].pk for call in compute.call_args_list}

    def test_an_unapplied_run_is_applied(self):
        scan = self._scan()

        self.assertEqual(self._run(), {scan.pk})

    def test_an_applied_run_reads_no_scan(self):
        self._scan({"applied_at": "2026-11-01T00:00:00+00:00"})
        self._scan({"attempts": 3})

        with patch("scanning.dots_mocr.live_analyze_jobs") as live:
            self.assertEqual(self._run(), set())

        live.assert_not_called()

    def test_the_ledger_is_the_live_runs(self):
        """An older run's applied stamp does not hold a new run back."""
        scan = self._scan({"applied_at": "2026-11-01T00:00:00+00:00"})
        ExternalJobFactory(
            scan=scan,
            stage=JobStage.ANALYZE,
            engine=JobEngine.DOTS_MOCR,
            status=JobStatus.CONSUMED,
            run=2,
        )

        self.assertEqual(self._run(), {scan.pk})


class TestVolumeGluePrefilter(ScanningTestCase):
    """The volume glues read only a live run they may glue (#612)."""

    def _walk(self):
        from scanning import jobs

        read = []

        def live_rows(scan):
            read.append(scan.pk)
            return jobs.live_run(scan, JobStage.DETECT, JobEngine.BLACKLETTER)

        yielded = [
            scan.pk
            for scan, _rows in jobs.ready_volume_runs(
                JobStage.DETECT,
                JobEngine.BLACKLETTER,
                JobProvider.RUNPOD,
                live_rows,
                lambda rows: 0,
                3,
            )
        ]
        return set(yielded), set(read)

    def _row(self, scan, run, status, shard=0, count=1):
        return ExternalJobFactory(
            scan=scan,
            run=run,
            status=status,
            shard_index=shard,
            shard_count=count,
        )

    def test_a_finished_live_run_is_glued(self):
        scan = ScanFactory()
        self._row(scan, 1, JobStatus.COMPLETED, 0, 2)
        self._row(scan, 1, JobStatus.CONSUMED, 1, 2)

        self.assertEqual(self._walk(), ({scan.pk}, {scan.pk}))

    def test_an_older_runs_completed_row_reads_no_rows(self):
        """The case that read a scan on every tick, forever."""
        glued = ScanFactory()
        self._row(glued, 1, JobStatus.COMPLETED)
        self._row(glued, 2, JobStatus.CONSUMED)
        waiting = ScanFactory()
        self._row(waiting, 1, JobStatus.COMPLETED)
        self._row(waiting, 2, JobStatus.IN_PROGRESS)

        self.assertEqual(self._walk(), (set(), set()))

    def test_an_unfinished_or_dead_live_run_reads_no_rows(self):
        for status in (JobStatus.PENDING, JobStatus.FAILED):
            scan = ScanFactory()
            self._row(scan, 1, JobStatus.COMPLETED, 0, 2)
            self._row(scan, 1, status, 1, 2)

        self.assertEqual(self._walk(), (set(), set()))

    def test_another_targets_rows_do_not_count(self):
        """An apply run's rows are not the volume's live run."""
        from scanning.models import ApplyRun

        scan = ScanFactory()
        self._row(scan, 1, JobStatus.COMPLETED)
        ExternalJobFactory(
            scan=scan,
            run=7,
            status=JobStatus.PENDING,
            apply_run=ApplyRun.objects.create(scan=scan, number=1),
        )

        self.assertEqual(self._walk(), ({scan.pk}, {scan.pk}))


class TestOpinionListIndexes(ScanningTestCase):
    """The opinions list's Pictures column and status filter (#599).

    Each index is pinned by its definition, and each query by the
    columns it filters, so the planner can read the index. The plan is
    not asserted: a test table is too small to measure its choice.
    """

    def _definition(self, name):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT indexdef FROM pg_indexes WHERE indexname = %s", [name]
            )
            (definition,) = cursor.fetchone()
        return definition

    def test_the_live_detections_of_a_run_by_page(self):
        definition = self._definition("idx_det_live_run_page")

        self.assertIn("(apply_run_id, page_index, label)", definition)
        self.assertIn("WHERE active", definition)

    def test_the_opinions_by_status(self):
        self.assertIn(
            "(status, id)", self._definition("idx_opinion_status_id")
        )

    def test_the_picture_count_reads_every_run_live_in_two_queries(self):
        """A page that spans several volumes costs two reads, not 1 + N."""
        from scanning import opinions
        from scanning.models import ApplyRun, Detection

        rows = []
        for page_index in (0, 1):
            scan = ScanFactory()
            run = ApplyRun.objects.create(scan=scan, number=1)
            rows.append(OpinionFactory(scan=scan, apply_run=run))
            Detection.objects.create(
                scan=scan,
                apply_run=run,
                page_index=page_index,
                label="IMAGE",
                label_id=0,
                confidence=0.9,
                x0=100.0,
                y0=100.0,
                x1=900.0,
                y1=900.0,
                img_width=1700,
                img_height=2200,
                source_page=page_index + 1,
            )

        with CaptureQueriesContext(connection) as captured:
            counts = opinions.picture_counts(rows)

        self.assertEqual(counts, {rows[0].pk: 1, rows[1].pk: 1})
        reads = [
            q["sql"] for q in captured if '"scanning_detection"' in q["sql"]
        ]
        self.assertEqual(len(reads), 2, reads)
        for sql in reads:
            with self.subTest(sql[:60]):
                self.assertIn('"scanning_detection"."active"', sql)
                self.assertIn('"scanning_detection"."apply_run_id"', sql)
        self.assertIn('"scanning_detection"."page_index" IN', reads[1])

    def test_a_status_filter_pages_newest_first(self):
        self.client.force_login(self.make_user())
        OpinionFactory(status=OpinionReviewStatus.ERROR)

        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(
                reverse("opinion_list"), {"status": "error"}
            )

        self.assertEqual(response.status_code, 200)
        page = [
            q["sql"]
            for q in captured
            if q["sql"].startswith('SELECT "scanning_opinion"."id"')
            and "LIMIT" in q["sql"]
        ]
        self.assertEqual(len(page), 1, page)
        self.assertIn(""""scanning_opinion"."status" = 'error'""", page[0])
        self.assertIn('ORDER BY "scanning_opinion"."id" DESC', page[0])
