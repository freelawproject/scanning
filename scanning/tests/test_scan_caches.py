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
"""

from unittest.mock import patch

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from scanning.factories import (
    ExternalJobFactory,
    OpinionFactory,
    OpinionScanFactory,
    ScanFactory,
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
