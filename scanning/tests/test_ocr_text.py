"""Tests for the text overlay of the process viewer (issue #262).

A reviewer presses one button and the viewer draws the text dots.mocr
read on each page of the viewport. The server half is one endpoint that
mints a presigned GET: the browser reads the document from the bucket,
so the web pod reads no byte of it. These tests pin that endpoint, the
key it chooses in each page space, and the flag that shows the button.
"""

from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from scanning import dots_mocr
from scanning.factories import ExternalJobFactory, ScanFactory
from scanning.models import (
    JobEngine,
    JobProvider,
    JobStage,
    JobStatus,
    Status,
)
from scanning.tests.test_views import ScanningTestCase
from scanning.tests.test_yolo_apply import glued_run
from scanning.views_process import (
    FINAL_VOLUME_NOT_READY_MESSAGE,
    GLUED_OUTPUT_PRESIGN_TTL,
    NO_READ_TEXT_MESSAGE,
    NO_S3_GLUED_OUTPUT_MESSAGE,
    OCR_TEXT_OBJECT_GONE_MESSAGE,
    dots_run_is_glued,
)

PRESIGNED = "https://bucket.example/ocr.json?signature"


def analyze_rows(scan, status=JobStatus.CONSUMED, run=1, count=2):
    """Create one dots.mocr run's rows for ``scan``.

    :param scan: The scan.
    :param status: The status every row takes.
    :param run: The run number.
    :param count: How many shards the run has.
    :returns: The rows.
    """
    return [
        ExternalJobFactory(
            scan=scan,
            stage=JobStage.ANALYZE,
            engine=JobEngine.DOTS_MOCR,
            provider=JobProvider.RUNPOD,
            status=status,
            run=run,
            shard_index=index,
            shard_count=count,
        )
        for index in range(count)
    ]


class TestGluedVolumeKey(TestCase):
    """``dots_mocr.glued_volume_key``: the key, or nothing."""

    def test_no_run_reads_as_nothing(self):
        self.assertIsNone(dots_mocr.glued_volume_key(ScanFactory()))

    def test_an_open_run_reads_as_nothing(self):
        scan = ScanFactory()
        analyze_rows(scan, status=JobStatus.COMPLETED)

        self.assertIsNone(dots_mocr.glued_volume_key(scan))

    def test_one_row_short_reads_as_nothing(self):
        scan = ScanFactory()
        rows = analyze_rows(scan)
        rows[1].status = JobStatus.COMPLETED
        rows[1].save(update_fields=["status"])

        self.assertIsNone(dots_mocr.glued_volume_key(scan))

    def test_a_glued_run_gives_its_key(self):
        scan = ScanFactory()
        analyze_rows(scan, run=3)

        self.assertEqual(
            dots_mocr.glued_volume_key(scan),
            dots_mocr.glued_result_key(scan, 3),
        )


class TestOcrTextUrl(ScanningTestCase):
    """``scan_ocr_text_url``: one presigned GET, and no download."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.scan = ScanFactory(
            page_count=2, status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )

    def url(self, space=None):
        """Return the endpoint's URL, in one space.

        :param space: ``"final"`` for the corrected volume.
        :returns: The URL.
        """
        url = reverse("scan_ocr_text_url", kwargs={"pk": self.scan.pk})
        return f"{url}?space={space}" if space else url

    def test_a_login_is_required(self):
        self.client.logout()

        response = self.client.get(self.url())

        self.assertEqual(response.status_code, 302)

    def test_a_glued_run_answers_the_volume_document(self):
        analyze_rows(self.scan, run=2)

        with (
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch("scanning.s3_sync.object_size", return_value=4096) as size,
            patch(
                "scanning.s3_sync.presign_get", return_value=PRESIGNED
            ) as presign,
            patch("scanning.s3_sync.download_json_object") as download,
        ):
            response = self.client.get(self.url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"url": PRESIGNED, "space": "original", "size": 4096},
        )
        key = dots_mocr.glued_result_key(self.scan, 2)
        size.assert_called_once_with(key)
        # No ``content_disposition``: a fetch reads the answer, it does
        # not save a file.
        presign.assert_called_once_with(key, GLUED_OUTPUT_PRESIGN_TTL)
        # The pod mints a URL. It never reads the document.
        download.assert_not_called()

    def test_a_volume_nobody_read_is_refused(self):
        with patch("scanning.s3_sync.s3_active", return_value=True):
            response = self.client.get(self.url())

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"], NO_READ_TEXT_MESSAGE)

    def test_no_s3_is_refused(self):
        analyze_rows(self.scan)

        with patch("scanning.s3_sync.s3_active", return_value=False):
            response = self.client.get(self.url())

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"], NO_S3_GLUED_OUTPUT_MESSAGE)

    def test_a_document_that_is_gone_is_refused(self):
        analyze_rows(self.scan)

        with (
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch("scanning.s3_sync.object_size", return_value=None),
        ):
            response = self.client.get(self.url())

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json()["error"], OCR_TEXT_OBJECT_GONE_MESSAGE
        )

    def test_the_final_space_reads_the_run(self):
        run = glued_run(self.scan)

        with (
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch("scanning.s3_sync.object_size", return_value=99),
            patch(
                "scanning.s3_sync.presign_get", return_value=PRESIGNED
            ) as presign,
        ):
            response = self.client.get(self.url("final"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["space"], "final")
        presign.assert_called_once_with(run.ocr_key, GLUED_OUTPUT_PRESIGN_TTL)

    def test_the_final_space_refuses_without_a_run(self):
        # The volume document exists, and it is not an answer for this
        # space: its pages are the original's (#269).
        analyze_rows(self.scan)

        with patch("scanning.s3_sync.s3_active", return_value=True):
            response = self.client.get(self.url("final"))

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["error"], FINAL_VOLUME_NOT_READY_MESSAGE
        )


class TestOcrTextButton(ScanningTestCase):
    """The flag that draws the button, and the page that carries it."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)

    def test_the_flag_follows_the_rows(self):
        self.assertFalse(dots_run_is_glued(None))
        self.assertFalse(
            dots_run_is_glued(
                {"total": 2, "statuses": {JobStatus.COMPLETED: 2}}
            )
        )
        self.assertFalse(
            dots_run_is_glued(
                {
                    "total": 2,
                    "statuses": {
                        JobStatus.CONSUMED: 1,
                        JobStatus.COMPLETED: 1,
                    },
                }
            )
        )
        self.assertTrue(
            dots_run_is_glued(
                {"total": 2, "statuses": {JobStatus.CONSUMED: 2}}
            )
        )

    def test_step_1_draws_the_button_only_for_a_glued_run(self):
        scan = ScanFactory(
            page_count=2, status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )
        url = reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=1"

        response = self.client.get(url)
        self.assertNotContains(response, 'id="ocr-text-toggle"')

        analyze_rows(scan)
        response = self.client.get(url)
        self.assertContains(response, 'id="ocr-text-toggle"')

    def test_step_2_draws_the_button_too(self):
        scan = ScanFactory(
            page_count=2, status=Status.READY_FOR_REDACTION_REVIEW
        )
        analyze_rows(scan)
        url = reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=2"

        response = self.client.get(url)

        self.assertContains(response, 'id="ocr-text-toggle"')
