"""Tests for review 2 reading the corrected volume (issue #269).

The apply (#224) writes the corrected volume under ``jobs/apply/a{n}/``
and glues the paid results into its page space. These tests pin the
readers of those outputs: the helpers in ``apply.py``, the page-number
lookup of ``detections.json``, the step-2 view and its routes, the
outputs index, the step-3 gate, and the data migration that names the
run each measured detection run was measured against.
"""

import importlib
import json
import pathlib
from unittest.mock import MagicMock, patch

from django.apps import apps as django_apps
from django.test import TestCase
from django.urls import reverse

from scanning import apply, review_states, s3_sync, services, yolo
from scanning.factories import ExternalJobFactory, ScanFactory
from scanning.models import ApplyRun, JobEngine, JobStage, JobStatus, Status
from scanning.tests.test_redaction_review import applied_scan
from scanning.tests.test_views import ScanningTestCase
from scanning.tests.test_yolo_apply import (
    PRINTED,
    glued_run,
    identity_map,
    merged_scan,
)
from scanning.utils import PIPELINE_PAUSED_MESSAGE
from scanning.views_api import GENERATE_REQUIRES_REDACTION_REVIEW_MESSAGE
from scanning.views_process import (
    FINAL_VOLUME_IS_ORIGINAL_MESSAGE,
    FINAL_VOLUME_NOT_READY_MESSAGE,
    PRINTED_PAGES_UNAVAILABLE_MESSAGE,
)


def edited_map() -> dict:
    """Return a stored map of a two-page volume whose page 1 was deleted
    and whose page 2 is followed by one inserted page.

    :returns: The map, in the shape of ``ApplyPlan.to_map``.
    """
    return {
        "schema_version": apply.MAP_SCHEMA_VERSION,
        "source_page_count": 2,
        "final_page_count": 2,
        "deleted_pages": [1],
        "pages": [
            {"final_page": 1, "source": {"kind": "original", "pdf_page": 2}},
            {
                "final_page": 2,
                "source": {
                    "kind": "edit",
                    "edit_id": 7,
                    "edit_kind": "insert_page",
                    "page": 0,
                    "reference_pdf_page": 2,
                },
            },
        ],
    }


class TestApplyReaders(TestCase):
    """The helpers of ``apply.py`` that read a run's outputs."""

    def test_viewer_pages_draws_one_placeholder_per_final_page(self):
        page_map, ocr_by_page = apply.viewer_pages(PRINTED)

        self.assertEqual(
            page_map,
            [
                {"type": "pdf_page", "pdf_index": 0, "logical_number": "101"},
                {
                    "type": "pdf_page",
                    "pdf_index": 1,
                    "logical_number": "102-103",
                },
            ],
        )
        self.assertEqual(ocr_by_page[1]["detected"], "101")
        self.assertEqual(ocr_by_page[1]["zone"], "read")
        self.assertEqual(ocr_by_page[2]["type"], "range")
        self.assertEqual(ocr_by_page[2]["zone"], "typed")
        self.assertIsNone(ocr_by_page[2]["score"])

    def test_a_page_with_no_printed_number_is_labelled_by_position(self):
        printed = {"pages": [{"final_page": 3, "printed": None, "type": None}]}

        page_map, ocr_by_page = apply.viewer_pages(printed)

        self.assertEqual(page_map[0]["logical_number"], 3)
        self.assertIsNone(ocr_by_page[3]["detected"])

    def test_positional_pages_needs_no_document(self):
        scan = ScanFactory(page_count=3)
        run = glued_run(scan)

        page_map, ocr_by_page = apply.positional_pages(run)

        self.assertEqual([e["pdf_index"] for e in page_map], [0, 1, 2])
        self.assertEqual(ocr_by_page, {})

    def test_page_number_lookup_is_in_the_final_space(self):
        self.assertEqual(
            apply.page_number_lookup(PRINTED), {0: (101, None), 1: (102, 103)}
        )

    def test_describe_map_counts_the_edits(self):
        self.assertEqual(
            apply.describe_map(edited_map()),
            {
                "final_page_count": 2,
                "deleted": 1,
                "inserted": 1,
                "replaced": 0,
                "rotated": 0,
                "identity": False,
            },
        )
        self.assertTrue(apply.describe_map(identity_map(2))["identity"])

    def test_local_copy_mirrors_the_prefix_under_the_output_dir(self):
        scan = ScanFactory(page_count=2)
        prefix = s3_sync.s3_processing_prefix(scan)
        key = f"{prefix}jobs/apply/a1/bitonal.pdf"
        expected = pathlib.Path(scan.output_dir) / "jobs/apply/a1/bitonal.pdf"

        def pull(key_arg, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"%PDF-1.4 final")

        with (
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch("scanning.s3_sync.download_object", side_effect=pull) as dl,
        ):
            first = apply.local_copy(scan, key)
            second = apply.local_copy(scan, key)

        self.assertEqual(first, expected)
        self.assertEqual(second, expected)
        # Pulled once: the second read finds the mirror.
        dl.assert_called_once()
        self.assertEqual(dl.call_args.args[0], key)

    def test_local_copy_refuses_a_key_outside_the_prefix(self):
        scan = ScanFactory(page_count=2)

        with self.assertRaises(apply.ApplyError):
            apply.local_copy(scan, "processing/999/x/1/1/bitonal.pdf")

    def test_local_copy_without_s3_needs_the_file(self):
        scan = ScanFactory(page_count=2)
        key = f"{s3_sync.s3_processing_prefix(scan)}jobs/apply/a1/bitonal.pdf"

        with (
            patch("scanning.s3_sync.s3_active", return_value=False),
            self.assertRaises(apply.ApplyError),
        ):
            apply.local_copy(scan, key)

    def test_load_printed_pages_refuses_another_shape(self):
        scan = ScanFactory(page_count=2)
        run = glued_run(scan)

        with (
            patch("scanning.s3_sync.download_json_object", return_value=[]),
            self.assertRaises(apply.ApplyError),
        ):
            apply.load_printed_pages(scan, run)


class TestPageNumberLookupResolves(TestCase):
    """``detections.json`` carries the numbers of the space its boxes
    are in, and six callers write it, so one resolver decides."""

    def test_a_measured_scan_reads_the_printed_pages(self):
        scan, _ = applied_scan()

        with patch.object(apply, "load_printed_pages", return_value=PRINTED):
            lookup = services._page_number_lookup(scan)

        self.assertEqual(lookup, {0: (101, None), 1: (102, 103)})

    def test_an_unmeasured_scan_reads_the_ocr_results(self):
        scan, _ = merged_scan(
            ocr_results=[{"pdf_page": 1, "detected": "7", "type": "single"}]
        )

        with patch.object(apply, "load_printed_pages") as load:
            lookup = services._page_number_lookup(scan)

        load.assert_not_called()
        self.assertEqual(lookup, {0: (7, None)})

    def test_a_document_the_caller_holds_wins(self):
        scan = ScanFactory(
            ocr_results=[{"pdf_page": 1, "detected": "7", "type": "single"}]
        )

        lookup = services._page_number_lookup(scan, PRINTED)

        self.assertEqual(lookup, {0: (101, None), 1: (102, 103)})

    def test_printed_page_span(self):
        self.assertEqual(
            services.printed_page_span("12", "single"), (12, None)
        )
        self.assertEqual(
            services.printed_page_span("12-14", "range"), (12, 14)
        )
        self.assertEqual(
            services.printed_page_span("12–14", "range"), (12, 14)
        )
        self.assertIsNone(services.printed_page_span("", "single"))
        self.assertIsNone(services.printed_page_span("x", "single"))
        self.assertIsNone(services.printed_page_span("12", "range"))


class TestServeFinalPdf(ScanningTestCase):
    """The bitonal copy of the corrected volume, for step 2."""

    def setUp(self):
        self.client.force_login(self.make_user())

    def _get(self, scan):
        return self.client.get(
            reverse("serve_final_pdf", kwargs={"pk": scan.pk})
        )

    def test_no_corrected_volume_is_a_409_that_offers_the_original(self):
        scan = ScanFactory(status=Status.READY_FOR_REDACTION_REVIEW)

        response = self._get(scan)

        self.assertEqual(response.status_code, 409)
        data = response.json()
        self.assertEqual(data["status"], "unavailable")
        self.assertEqual(data["message"], FINAL_VOLUME_NOT_READY_MESSAGE)
        self.assertTrue(data["original_available"])

    def test_the_copy_is_streamed_from_its_mirror_and_pulled_once(self):
        scan, _ = applied_scan(status=Status.READY_FOR_REDACTION_REVIEW)
        run = apply.current_run(scan)
        prefix = s3_sync.s3_processing_prefix(scan)
        ApplyRun.objects.filter(pk=run.pk).update(
            bitonal_key=f"{prefix}jobs/apply/a1/bitonal.pdf"
        )

        def pull(key, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"%PDF-1.4 final")

        with (
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch("scanning.s3_sync.download_object", side_effect=pull) as dl,
        ):
            first = self._get(scan)
            body = b"".join(first.streaming_content)
            second = self._get(scan)
            b"".join(second.streaming_content)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first["X-Scan-Preview"], "bitonal")
        self.assertEqual(body, b"%PDF-1.4 final")
        self.assertEqual(second.status_code, 200)
        dl.assert_called_once()

    def test_a_copy_that_is_the_original_is_refused(self):
        """A 1-bit upload skips the conversion, so the run's bitonal
        copy is the multi-GB original, which #185 keeps out of this
        stream. The viewer offers the original load instead."""
        scan, _ = applied_scan(status=Status.READY_FOR_REDACTION_REVIEW)
        ApplyRun.objects.filter(scan=scan).update(
            bitonal_key=s3_sync.s3_original_key(scan)
        )

        with patch("scanning.s3_sync.download_object") as dl:
            response = self._get(scan)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["message"], FINAL_VOLUME_IS_ORIGINAL_MESSAGE
        )
        self.assertTrue(response.json()["original_available"])
        dl.assert_not_called()

    def test_a_pull_that_fails_is_a_409_with_a_reload_hint(self):
        scan, _ = applied_scan(status=Status.READY_FOR_REDACTION_REVIEW)
        prefix = s3_sync.s3_processing_prefix(scan)
        ApplyRun.objects.filter(scan=scan).update(
            bitonal_key=f"{prefix}jobs/apply/a1/bitonal.pdf"
        )

        with (
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch(
                "scanning.s3_sync.download_object",
                side_effect=RuntimeError("gone"),
            ),
        ):
            response = self._get(scan)

        self.assertEqual(response.status_code, 409)
        self.assertIn("Reload", response.json()["message"])


class TestScanOriginalUrlFinalSpace(ScanningTestCase):
    """``?space=final`` hands the viewer the run's final PDF."""

    def setUp(self):
        self.client.force_login(self.make_user())

    def _get(self, scan):
        return self.client.get(
            reverse("scan_original_url", kwargs={"pk": scan.pk}),
            {"space": "final"},
        )

    def test_the_final_pdf_is_presigned(self):
        scan, _ = applied_scan()
        run = apply.current_run(scan)

        with (
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch(
                "scanning.s3_sync.presign_get", return_value="https://x/final"
            ) as presign,
        ):
            response = self._get(scan)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"url": "https://x/final", "embedded_whole": False},
        )
        self.assertEqual(presign.call_args.args[0], run.final_pdf_key)

    def test_no_corrected_volume_is_a_409(self):
        scan = ScanFactory(status=Status.READY_FOR_REDACTION_REVIEW)

        with patch("scanning.s3_sync.s3_active", return_value=True):
            response = self._get(scan)

        self.assertEqual(response.status_code, 409)

    def test_without_s3_there_is_no_final_pdf(self):
        scan, _ = applied_scan()

        with patch("scanning.s3_sync.s3_active", return_value=False):
            response = self._get(scan)

        self.assertEqual(response.status_code, 409)

    def test_without_the_flag_the_original_is_answered(self):
        scan, _ = applied_scan()

        with patch(
            "scanning.s3_sync.presign_original_get", return_value="https://x/o"
        ):
            response = self.client.get(
                reverse("scan_original_url", kwargs={"pk": scan.pk})
            )

        self.assertEqual(response.json()["url"], "https://x/o")


class TestServeOriginalCropFinalSpace(ScanningTestCase):
    """A crop asked for a page of the corrected volume."""

    def setUp(self):
        self.client.force_login(self.make_user())

    def _crop(self, scan, page):
        return self.client.get(
            reverse("serve_original_crop", kwargs={"pk": scan.pk}),
            {
                "page": page,
                "x0": 0,
                "y0": 0,
                "x1": 10,
                "y1": 10,
                "space": "final",
            },
        )

    def _fake_fitz(self):
        doc = MagicMock()
        doc.page_count = 2
        doc.__enter__ = lambda s: s
        doc.__exit__ = lambda s, *a: None
        page = MagicMock()
        page.get_pixmap.return_value.tobytes.return_value = b"png"
        doc.__getitem__ = MagicMock(return_value=page)
        return doc

    def test_a_kept_page_is_cropped_from_its_original_page(self):
        scan, _ = applied_scan()
        ApplyRun.objects.filter(scan=scan).update(page_map=edited_map())
        doc = self._fake_fitz()

        with (
            patch(
                "scanning.views_process.local_original_pdf",
                return_value="/x.pdf",
            ),
            patch("scanning.views_process.fitz.open", return_value=doc),
        ):
            response = self._crop(scan, 0)

        self.assertEqual(response.status_code, 200)
        # Final page 1 is original page 2: fitz index 1.
        doc.__getitem__.assert_called_once_with(1)

    def test_an_added_page_has_no_crop_in_the_original(self):
        scan, _ = applied_scan()
        ApplyRun.objects.filter(scan=scan).update(page_map=edited_map())

        with patch("scanning.views_process.local_original_pdf") as original:
            response = self._crop(scan, 1)

        self.assertEqual(response.status_code, 404)
        original.assert_not_called()

    def test_a_page_outside_the_volume_is_404(self):
        scan, _ = applied_scan()

        self.assertEqual(self._crop(scan, 5).status_code, 404)

    def test_without_a_corrected_volume_the_flag_is_404(self):
        scan = ScanFactory(page_count=2)

        self.assertEqual(self._crop(scan, 0).status_code, 404)


class TestStepTwoShowsTheCorrectedVolume(ScanningTestCase):
    """What the process page draws in the final space."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)

    def _page(self, scan, step=2):
        return self.client.get(
            reverse("scan_process", kwargs={"pk": scan.pk}), {"step": step}
        )

    def test_a_measured_volume_draws_the_final_space(self):
        scan, _ = applied_scan(status=Status.READY_FOR_REDACTION_REVIEW)

        with patch.object(apply, "load_printed_pages", return_value=PRINTED):
            response = self._page(scan)

        self.assertEqual(response.status_code, 200)
        context = response.context
        self.assertTrue(context["final_space"])
        self.assertTrue(context["page_edits_locked"])
        page_map = json.loads(context["page_map_json"])
        self.assertEqual(
            [e["logical_number"] for e in page_map], ["101", "102-103"]
        )
        self.assertEqual(json.loads(context["flagged_indices_json"]), [])
        self.assertEqual(json.loads(context["deleted_pages_json"]), [])
        self.assertEqual(json.loads(context["replaced_pages_json"]), {})
        html = response.content.decode()
        self.assertIn(reverse("serve_final_pdf", kwargs={"pk": scan.pk}), html)
        self.assertIn("finalSpace: true", html)
        self.assertIn("Showing the volume as uploaded", html)

    def test_a_volume_with_edits_says_what_changed(self):
        scan, _ = applied_scan(status=Status.READY_FOR_REDACTION_REVIEW)
        ApplyRun.objects.filter(scan=scan).update(page_map=edited_map())

        with patch.object(apply, "load_printed_pages", return_value=PRINTED):
            html = self._page(scan).content.decode()

        self.assertIn("Showing the corrected volume a1", html)
        self.assertIn("1 deleted, 1 inserted", html)

    def test_step_one_keeps_the_original_space(self):
        """Every ``PageEdit`` address is a page of the original."""
        scan, _ = applied_scan(status=Status.READY_FOR_REDACTION_REVIEW)

        with patch.object(apply, "load_printed_pages") as load:
            response = self._page(scan, step=1)

        load.assert_not_called()
        self.assertFalse(response.context["final_space"])
        html = response.content.decode()
        self.assertIn(reverse("serve_scan_pdf", kwargs={"pk": scan.pk}), html)
        self.assertIn("finalSpace: false", html)

    def test_a_complete_but_unmeasured_run_keeps_the_review_one_copy(self):
        """A final PDF under boxes of the original's space is the one
        thing the page must never show."""
        scan, _ = applied_scan(status=Status.READY_FOR_REDACTION_REVIEW)
        apply.supersede_runs(scan, "test")
        glued_run(scan, number=2)

        response = self._page(scan)

        self.assertFalse(response.context["final_space"])
        html = response.content.decode()
        self.assertIn(reverse("serve_scan_pdf", kwargs={"pk": scan.pk}), html)
        self.assertIn("a2 is built", html)
        self.assertIn("being measured", html)

    def test_no_run_says_the_volume_is_not_built(self):
        scan, _ = merged_scan(status=Status.PAGE_COMPLETENESS_REVIEW_DONE)
        ApplyRun.objects.filter(scan=scan).delete()

        html = self._page(scan).content.decode()

        self.assertIn("not built yet", html)

    def test_a_legacy_volume_gets_no_note(self):
        scan = ScanFactory(status=Status.PENDING_REVIEW, page_count=2)

        html = self._page(scan).content.decode()

        self.assertNotIn("not built yet", html)
        self.assertNotIn("Showing the", html)

    def test_a_printed_pages_read_that_fails_keeps_the_page(self):
        scan, _ = applied_scan(status=Status.READY_FOR_REDACTION_REVIEW)

        with patch.object(
            apply, "load_printed_pages", side_effect=apply.ApplyError("gone")
        ):
            response = self._page(scan)

        self.assertEqual(response.status_code, 200)
        page_map = json.loads(response.context["page_map_json"])
        self.assertEqual([e["pdf_index"] for e in page_map], [0, 1])
        self.assertIn(
            PRINTED_PAGES_UNAVAILABLE_MESSAGE,
            response.context["detect_warnings"],
        )

    def test_the_files_link_is_staff_only(self):
        scan, _ = applied_scan(status=Status.READY_FOR_REDACTION_REVIEW)
        url = reverse("apply_output_index", kwargs={"pk": scan.pk})

        with patch.object(apply, "load_printed_pages", return_value=PRINTED):
            self.assertNotIn(url, self._page(scan).content.decode())
            self.client.force_login(self.make_staff_user())
            self.assertIn(url, self._page(scan).content.decode())

    def test_the_fragment_and_the_page_agree(self):
        scan, _ = applied_scan(status=Status.READY_FOR_REDACTION_REVIEW)
        ApplyRun.objects.filter(scan=scan).update(page_map=edited_map())

        response = self.client.get(
            reverse("process_actions", kwargs={"pk": scan.pk}), {"step": 2}
        )

        self.assertIn(
            "Showing the corrected volume a1", response.json()["html"]
        )


class TestApplyOutputs(ScanningTestCase):
    """The outputs of the apply runs, by scan id (the #243 shape)."""

    def setUp(self):
        self.client.force_login(self.make_user())

    def test_the_index_lists_the_runs_their_files_and_their_shards(self):
        scan, _ = applied_scan()
        run = apply.current_run(scan)
        row = ExternalJobFactory(
            scan=scan,
            stage=JobStage.ANALYZE,
            engine=JobEngine.DOTS_MOCR,
            status=JobStatus.CONSUMED,
            apply_run=run,
            run=5,
            input_manifest={"edit_id": 7, "page_count": 1},
            result_key="jobs/apply/a1/analyze/dots_mocr/r5-s0-a1.json",
        )

        response = self.client.get(
            reverse("apply_output_index", kwargs={"pk": scan.pk})
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["standing"], "a1")
        entry = data["runs"][0]
        self.assertEqual(entry["label"], "a1")
        self.assertTrue(entry["complete"])
        self.assertTrue(entry["measured"])
        self.assertTrue(entry["identity"])
        self.assertEqual(
            set(entry["files"]),
            {
                "final-pdf",
                "bitonal",
                "ocr-volume",
                "printed-pages",
                "detections-volume",
                "page-map",
            },
        )
        self.assertEqual(entry["shards"][0]["edit_id"], 7)
        self.assertEqual(
            entry["shards"][0]["url"],
            reverse(
                "serve_apply_shard",
                kwargs={"pk": scan.pk, "number": 1, "row_pk": row.pk},
            ),
        )

    def test_a_scan_with_no_run_gets_an_empty_list(self):
        scan = ScanFactory(page_count=2)

        data = self.client.get(
            reverse("apply_output_index", kwargs={"pk": scan.pk})
        ).json()

        self.assertEqual(data, {"scan": scan.pk, "standing": None, "runs": []})

    def test_an_output_redirects_to_a_presigned_get(self):
        scan, _ = applied_scan()
        run = apply.current_run(scan)

        with (
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch("scanning.s3_sync.object_exists", return_value=True),
            patch(
                "scanning.s3_sync.presign_get", return_value="https://x/y"
            ) as presign,
        ):
            response = self.client.get(
                reverse(
                    "serve_apply_output",
                    kwargs={"pk": scan.pk, "number": 1, "output": "bitonal"},
                )
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(presign.call_args.args[0], run.bitonal_key)
        self.assertEqual(
            presign.call_args.kwargs["content_disposition"],
            f'attachment; filename="scan-{scan.pk}-apply-a1-bitonal.pdf"',
        )

    def test_the_page_map_is_beside_the_outputs(self):
        scan, _ = applied_scan()
        run = apply.current_run(scan)

        with (
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch("scanning.s3_sync.object_exists", return_value=True),
            patch(
                "scanning.s3_sync.presign_get", return_value="https://x/y"
            ) as presign,
        ):
            self.client.get(
                reverse(
                    "serve_apply_output",
                    kwargs={"pk": scan.pk, "number": 1, "output": "page-map"},
                )
            )

        self.assertEqual(
            presign.call_args.args[0],
            f"{apply.run_prefix(scan, run)}page_map.json",
        )

    def test_a_blank_key_and_an_unknown_slug_are_404s(self):
        scan, _ = applied_scan()
        ApplyRun.objects.filter(scan=scan).update(detections_key="")

        blank = self.client.get(
            reverse(
                "serve_apply_output",
                kwargs={
                    "pk": scan.pk,
                    "number": 1,
                    "output": "detections-volume",
                },
            )
        )
        unknown = self.client.get(
            reverse(
                "serve_apply_output",
                kwargs={"pk": scan.pk, "number": 1, "output": "nope"},
            )
        )
        missing = self.client.get(
            reverse(
                "serve_apply_output",
                kwargs={"pk": scan.pk, "number": 9, "output": "bitonal"},
            )
        )

        self.assertEqual(blank.status_code, 404)
        self.assertIn("not written", blank.json()["error"])
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(missing.status_code, 404)

    def test_a_shard_redirects_to_the_row_result(self):
        scan, _ = applied_scan()
        run = apply.current_run(scan)
        row = ExternalJobFactory(
            scan=scan,
            status=JobStatus.CONSUMED,
            apply_run=run,
            run=5,
            input_manifest={"edit_id": 7},
            result_key="jobs/apply/a1/detect/blackletter/r5-s0-a1.json",
        )

        with (
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch("scanning.s3_sync.object_exists", return_value=True),
            patch(
                "scanning.s3_sync.presign_get", return_value="https://x/y"
            ) as presign,
        ):
            response = self.client.get(
                reverse(
                    "serve_apply_shard",
                    kwargs={"pk": scan.pk, "number": 1, "row_pk": row.pk},
                )
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(presign.call_args.args[0], row.result_key)

    def test_the_routes_need_a_login(self):
        scan, _ = applied_scan()
        self.client.logout()

        response = self.client.get(
            reverse("apply_output_index", kwargs={"pk": scan.pk})
        )

        self.assertEqual(response.status_code, 302)


class TestGenerateFilesGate(ScanningTestCase):
    """The review-2 approval is checked in the view, not only in the bar."""

    def setUp(self):
        self.client.force_login(self.make_user())

    def _post(self, scan):
        return self.client.post(
            reverse("generate_files", kwargs={"pk": scan.pk}), follow=True
        )

    def test_an_unapproved_volume_is_sent_back_to_step_two(self):
        scan = ScanFactory(status=Status.READY_FOR_REDACTION_REVIEW)

        response = self._post(scan)

        self.assertEqual(response.redirect_chain[0][0].split("?")[1], "step=2")
        messages = [str(m) for m in response.context["messages"]]
        self.assertIn(GENERATE_REQUIRES_REDACTION_REVIEW_MESSAGE, messages)

    def test_an_approved_volume_reaches_the_paused_answer(self):
        scan = ScanFactory(status=Status.REDACTION_REVIEW_DONE)

        response = self._post(scan)

        messages = [str(m) for m in response.context["messages"]]
        self.assertNotIn(GENERATE_REQUIRES_REDACTION_REVIEW_MESSAGE, messages)
        self.assertIn(PIPELINE_PAUSED_MESSAGE, messages)

    def test_a_legacy_volume_keeps_its_way_in(self):
        scan = ScanFactory(status=Status.PENDING_REVIEW)

        response = self._post(scan)

        messages = [str(m) for m in response.context["messages"]]
        self.assertNotIn(GENERATE_REQUIRES_REDACTION_REVIEW_MESSAGE, messages)


class TestStampMigration(TestCase):
    """The data migration names the run of every stamp that may stand."""

    def _stamp(self):
        module = importlib.import_module(
            "scanning.migrations.0024_stamp_redaction_apply_run"
        )
        module.stamp_apply_runs(django_apps, None)

    def _old_stamp(self, rows):
        state = yolo.apply_state(rows)
        state.pop("apply_run", None)
        state["applied_at"] = "2026-09-01T00:00:00"
        yolo.write_apply_state(rows, state)

    def test_an_identity_run_is_stamped(self):
        scan, rows = merged_scan()
        self._old_stamp(rows)

        self._stamp()

        run = apply.current_run(scan)
        self.assertTrue(
            yolo.redactions_current(yolo.live_detect_jobs(scan), run)
        )

    def test_a_run_with_edits_is_left_for_the_compute(self):
        scan, rows = merged_scan()
        self._old_stamp(rows)
        ApplyRun.objects.filter(scan=scan).update(page_map=edited_map())

        self._stamp()

        state = yolo.apply_state(yolo.live_detect_jobs(scan))
        self.assertNotIn("apply_run", state)
        with patch("scanning.s3_sync.s3_active", return_value=True):
            self.assertEqual(yolo.queue_ready_runs(), 1)

    def test_a_run_of_another_original_is_left_alone(self):
        scan, rows = merged_scan(source_fingerprint="200:2")
        self._old_stamp(rows)
        ApplyRun.objects.filter(scan=scan).update(source_fingerprint="100:2")

        self._stamp()

        self.assertNotIn(
            "apply_run", yolo.apply_state(yolo.live_detect_jobs(scan))
        )

    def test_a_stamp_that_names_a_run_is_kept(self):
        scan, rows = applied_scan()
        apply.supersede_runs(scan, "test")
        glued_run(scan, number=2)
        before = yolo.apply_state(yolo.live_detect_jobs(scan))

        self._stamp()

        self.assertEqual(yolo.apply_state(yolo.live_detect_jobs(scan)), before)

    def test_final_volume_ready_still_answers_yes_or_no(self):
        scan, _ = merged_scan()

        self.assertTrue(review_states.final_volume_ready(scan))
        ApplyRun.objects.filter(scan=scan).update(ocr_key="")
        self.assertFalse(review_states.final_volume_ready(scan))
