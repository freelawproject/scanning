"""Tests for the text overlay of the process viewer (#262, #381).

A reviewer picks an OCR engine and presses one button, and the viewer
draws the text that engine read on each page of the viewport. The
server half is one endpoint that mints a presigned GET: the browser
reads the document from the bucket, so the web pod reads no byte of it.
These tests pin that endpoint, the key it chooses per engine and per
page space, the list that fills the dropdown, and the rule that the
browser holds no engine name.
"""

import re
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from scanning import dots_mocr, opinion_ocr
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
    NO_READ_FINAL_TEXT_MESSAGE,
    NO_READ_TEXT_MESSAGE,
    NO_S3_GLUED_OUTPUT_MESSAGE,
    OCR_TEXT_OBJECT_GONE_MESSAGE,
    UNKNOWN_OCR_ENGINE_MESSAGE,
    engine_label,
    ocr_text_engines,
    run_is_glued,
)

PRESIGNED = "https://bucket.example/ocr.json?signature"

#: Where each engine's volume rows live. The test names them, because a
#: fixture that read them off the code under test would pin nothing.
ENGINE_ROWS = {
    "dots_mocr": (JobStage.ANALYZE, JobEngine.DOTS_MOCR),
    "mistral_ocr": (JobStage.EXTRACT, JobEngine.MISTRAL_OCR),
    "surya": (JobStage.EXTRACT, JobEngine.SURYA),
}


def engine_rows(
    scan, name="dots_mocr", status=JobStatus.CONSUMED, run=1, count=2
):
    """Create one engine's volume run for ``scan``.

    :param scan: The scan.
    :param name: A key of ``opinion_ocr.ENGINES``.
    :param status: The status every row takes.
    :param run: The run number.
    :param count: How many shards the run has.
    :returns: The rows.
    """
    stage, engine = ENGINE_ROWS[name]
    return [
        ExternalJobFactory(
            scan=scan,
            stage=stage,
            engine=engine,
            provider=JobProvider.RUNPOD,
            status=status,
            run=run,
            shard_index=index,
            shard_count=count,
        )
        for index in range(count)
    ]


def analyze_rows(scan, status=JobStatus.CONSUMED, run=1, count=2):
    """Create one dots.mocr run's rows for ``scan``.

    :param scan: The scan.
    :param status: The status every row takes.
    :param run: The run number.
    :param count: How many shards the run has.
    :returns: The rows.
    """
    return engine_rows(scan, "dots_mocr", status, run, count)


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

    def url(self, space=None, engine=None):
        """Return the endpoint's URL, in one space and for one engine.

        :param space: ``"final"`` for the corrected volume.
        :param engine: A key of ``opinion_ocr.ENGINES``; the endpoint's
            own default when omitted.
        :returns: The URL.
        """
        url = reverse("scan_ocr_text_url", kwargs={"pk": self.scan.pk})
        query = []
        if engine:
            query.append(f"engine={engine}")
        if space:
            query.append(f"space={space}")
        return f"{url}?{'&'.join(query)}" if query else url

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
            {
                "url": PRESIGNED,
                "space": "original",
                "size": 4096,
                "engine": "dots_mocr",
                "label": "dots.mocr",
                "fields": {
                    "units": "cells",
                    "text": "text",
                    "type": "category",
                },
            },
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
        self.assertEqual(
            response.json()["error"],
            NO_READ_TEXT_MESSAGE.format(label="dots.mocr"),
        )

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
            response.json()["error"],
            OCR_TEXT_OBJECT_GONE_MESSAGE.format(label="dots.mocr"),
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

    def test_every_engine_answers_its_own_volume_document(self):
        """Each engine reads the key of its own glued run (#381)."""
        for name, spec in opinion_ocr.ENGINES.items():
            with self.subTest(engine=name):
                scan = ScanFactory(page_count=2)
                self.scan = scan
                engine_rows(scan, name, run=4)

                with (
                    patch("scanning.s3_sync.s3_active", return_value=True),
                    patch("scanning.s3_sync.object_size", return_value=7),
                    patch(
                        "scanning.s3_sync.presign_get", return_value=PRESIGNED
                    ) as presign,
                ):
                    response = self.client.get(self.url(engine=name))

                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["engine"], name)
                self.assertEqual(body["label"], engine_label(name))
                self.assertEqual(body["fields"], spec.fields)
                presign.assert_called_once_with(
                    spec.module.glued_result_key(scan, 4),
                    GLUED_OUTPUT_PRESIGN_TTL,
                )

    def test_an_engine_that_read_nothing_names_itself(self):
        """The refusal says which engine, because three are offered."""
        analyze_rows(self.scan)

        with patch("scanning.s3_sync.s3_active", return_value=True):
            response = self.client.get(self.url(engine="surya"))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json()["error"],
            NO_READ_TEXT_MESSAGE.format(label="Surya"),
        )

    def test_an_unknown_engine_is_refused(self):
        """A name outside the table is a 400 that lists the names."""
        analyze_rows(self.scan)

        with patch("scanning.s3_sync.s3_active", return_value=True):
            response = self.client.get(self.url(engine="paddle"))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["error"],
            UNKNOWN_OCR_ENGINE_MESSAGE.format(
                engine="paddle", known=", ".join(opinion_ocr.ENGINES)
            ),
        )

    def test_the_final_space_reads_each_engine_key(self):
        """The corrected volume's document is the run's own field."""
        run = glued_run(self.scan)
        run.extract_key = "processing/1/extract-1.json"
        run.save(update_fields=["extract_key"])

        for name, expected in (
            ("dots_mocr", run.ocr_key),
            ("mistral_ocr", run.extract_key),
        ):
            with self.subTest(engine=name):
                with (
                    patch("scanning.s3_sync.s3_active", return_value=True),
                    patch("scanning.s3_sync.object_size", return_value=99),
                    patch(
                        "scanning.s3_sync.presign_get", return_value=PRESIGNED
                    ) as presign,
                ):
                    response = self.client.get(self.url("final", engine=name))

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["space"], "final")
                presign.assert_called_once_with(
                    expected, GLUED_OUTPUT_PRESIGN_TTL
                )

    def test_the_final_space_refuses_an_engine_that_did_not_read_it(self):
        """A corrected volume Surya never read is that engine's fault.

        ``ApplyRun.is_complete`` counts neither ``extract_key`` nor
        ``surya_key`` (#245, #368), so the volume is ready and the
        engine is not. That is a 404 about the engine, not the 409
        about the volume.
        """
        glued_run(self.scan)

        with patch("scanning.s3_sync.s3_active", return_value=True):
            response = self.client.get(self.url("final", engine="surya"))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json()["error"],
            NO_READ_FINAL_TEXT_MESSAGE.format(label="Surya"),
        )


class TestOcrTextEngines(TestCase):
    """``ocr_text_engines``: what the dropdown offers (#381)."""

    def setUp(self):
        self.scan = ScanFactory(page_count=2)

    def summaries(self, *read):
        """Return the summaries of the engines that are glued.

        :param read: The engine names whose run is glued.
        :returns: ``{engine name: summary or None}``.
        """
        glued = {"total": 2, "statuses": {JobStatus.CONSUMED: 2}}
        return {
            name: (glued if name in read else None)
            for name in opinion_ocr.ENGINES
        }

    def test_the_order_is_the_table_and_dots_is_the_default(self):
        entries = ocr_text_engines(
            self.summaries(*opinion_ocr.ENGINES), None, False
        )

        self.assertEqual(
            [entry["name"] for entry in entries],
            list(opinion_ocr.ENGINES),
        )
        self.assertTrue(all(entry["available"] for entry in entries))
        self.assertEqual(
            [entry["selected"] for entry in entries], [True, False, False]
        )

    def test_an_engine_that_read_nothing_is_offered_and_marked(self):
        entries = ocr_text_engines(self.summaries("surya"), None, False)

        self.assertEqual(len(entries), len(opinion_ocr.ENGINES))
        by_name = {entry["name"]: entry for entry in entries}
        self.assertFalse(by_name["dots_mocr"]["available"])
        self.assertTrue(by_name["surya"]["available"])
        # The select opens on its first option whether it is disabled
        # or not, so the first engine that read is the selected one.
        self.assertTrue(by_name["surya"]["selected"])
        self.assertFalse(by_name["dots_mocr"]["selected"])

    def test_the_final_space_reads_the_run_and_not_the_rows(self):
        run = glued_run(self.scan)

        entries = ocr_text_engines(
            self.summaries(*opinion_ocr.ENGINES), run, True
        )

        by_name = {entry["name"]: entry for entry in entries}
        self.assertTrue(by_name["dots_mocr"]["available"])
        self.assertFalse(by_name["mistral_ocr"]["available"])
        self.assertFalse(by_name["surya"]["available"])

    def test_no_run_reads_as_nothing_in_the_final_space(self):
        entries = ocr_text_engines(
            self.summaries(*opinion_ocr.ENGINES), None, True
        )

        self.assertFalse(any(entry["available"] for entry in entries))


class TestOcrTextButton(ScanningTestCase):
    """The flag that draws the button, and the page that carries it."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)

    def test_the_flag_follows_the_rows(self):
        self.assertFalse(run_is_glued(None))
        self.assertFalse(
            run_is_glued({"total": 2, "statuses": {JobStatus.COMPLETED: 2}})
        )
        self.assertFalse(
            run_is_glued(
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
            run_is_glued({"total": 2, "statuses": {JobStatus.CONSUMED: 2}})
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

    def test_the_select_offers_every_engine_and_marks_the_read_ones(self):
        """The dropdown of #381: three options, one of them live."""
        scan = ScanFactory(
            page_count=2, status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )
        engine_rows(scan, "mistral_ocr")
        url = reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=1"

        response = self.client.get(url)

        self.assertContains(response, 'id="ocr-text-engine"')
        for name, spec in opinion_ocr.ENGINES.items():
            with self.subTest(engine=name):
                self.assertContains(response, f'value="{name}"')
                self.assertContains(response, engine_label(name))
        # dots.mocr read nothing here, so it is offered and refused,
        # and the engine that did read is the one the select opens on.
        self.assertContains(
            response, '<option value="dots_mocr" disabled>', html=False
        )
        self.assertContains(
            response, '<option value="mistral_ocr" selected>', html=False
        )
        self.assertContains(response, "(not read)")

    def test_a_volume_nobody_read_draws_neither_control(self):
        scan = ScanFactory(
            page_count=2, status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )
        url = reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=1"

        response = self.client.get(url)

        self.assertNotContains(response, 'id="ocr-text-engine"')
        self.assertNotContains(response, 'id="ocr-text-toggle"')


class TestTheBrowserNamesNoEngine(TestCase):
    """``ocr_text.js`` holds no copy of the engine table (#381).

    The endpoint answers the field names of the document it points at,
    so the script reads a document it knows nothing about. A name that
    crept into the file would be a second copy of
    ``opinion_ocr.ENGINES``, and a fourth engine would then need a
    script change nobody would remember to make. The twin of
    ``test_viewer_labels``, in the other direction.
    """

    def code(self) -> str:
        """Return the script with its comments removed.

        The prose may name an engine, and does: a reader of the module
        needs to know which documents it draws. The **code** may not.

        :returns: The script, comments stripped.
        :rtype: str
        """
        source = (
            Path(__file__).resolve().parent.parent
            / "static"
            / "scanning"
            / "ocr_text.js"
        ).read_text()
        source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
        return "\n".join(
            line
            for line in source.splitlines()
            if not line.strip().startswith("//")
        )

    def test_the_script_names_no_engine(self):
        code = self.code()

        for name, spec in opinion_ocr.ENGINES.items():
            with self.subTest(engine=name):
                self.assertNotIn(name, code)
                self.assertNotIn(engine_label(name), code)
                self.assertNotIn(f'"{spec.units_key}"', code)
                self.assertNotIn(f"'{spec.units_key}'", code)
                self.assertNotIn(f'"{spec.type_key}"', code)
                self.assertNotIn(f"'{spec.type_key}'", code)
