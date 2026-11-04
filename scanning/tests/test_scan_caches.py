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
