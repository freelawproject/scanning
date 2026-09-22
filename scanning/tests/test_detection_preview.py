"""Tests for the read-only detection preview of step 2 (issue #388).

Four groups, one per part of the design:

- the rule (:func:`review_states.preview_only`): which volume gets a
  preview, and every state that takes it away again;
- the read (``yolo.preview_entries`` and the three JSON endpoints the
  viewer asks), which answers the merged detection document and no row;
- the refusal (``views_api._refuse_preview``): every write of step 2
  under a preview;
- the page: the disclaimer, the locks and the links.
"""

import json
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from scanning import apply, review_states, yolo
from scanning.factories import ScanFactory
from scanning.models import (
    ApplyRun,
    Detection,
    ExternalJob,
    JobStatus,
    PageEdit,
    Scan,
    Status,
)
from scanning.tests.test_jobs import make_manifest
from scanning.tests.test_views import ScanningTestCase
from scanning.tests.test_yolo_apply import glued_run, merged_scan
from scanning.views_api import PREVIEW_READ_ONLY_MESSAGE


def previewable_scan(
    status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW, **kwargs
):
    """Build a scan whose detection run is merged and whose review is open.

    Every condition of the preview: a merged run, a review-1 status and
    no apply run, which is a volume nobody has approved yet.

    :param status: The status to give the scan.
    :param kwargs: Extra fields for the factory.
    :returns: ``(scan, rows)``.
    """
    kwargs.setdefault(
        "page_map",
        [
            {"type": "pdf_page", "pdf_index": 0, "logical_number": 1},
            {"type": "pdf_page", "pdf_index": 1, "logical_number": 2},
        ],
    )
    scan = ScanFactory(page_count=2, status=status, **kwargs)
    yolo.ensure_detect_jobs(scan, make_manifest(2, 1))
    ExternalJob.objects.filter(scan=scan).update(status=JobStatus.CONSUMED)
    return scan, yolo.live_detect_jobs(scan)


def merged_document(scan, detections=None):
    """Return a merged detection document for ``scan``.

    The shape ``yolo.merge_detect_results`` uploads, cut down to what
    the preview reads.

    :param scan: The scan the document describes.
    :param detections: The boxes; one headnote on the second page when
        omitted.
    :returns: The document.
    :rtype: dict
    """
    if detections is None:
        detections = [
            {
                "page_index": 1,
                "pdf_page": 2,
                "label": "HEADNOTE",
                "label_id": 3,
                "confidence": 0.91,
                "bbox": [10, 20, 30, 40],
                "img_width": 1700,
                "img_height": 2200,
                "model_count": 1,
                "found_by": ["bl_warm"],
            }
        ]
    return {
        "schema_version": yolo.MERGE_SCHEMA_VERSION,
        "scan_pk": scan.pk,
        "source_fingerprint": scan.source_fingerprint,
        "detections": detections,
    }


class TestThePreviewRule(TestCase):
    """Which volume has a preview, and which does not."""

    def test_a_merged_run_in_an_open_page_review_has_one(self):
        scan, rows = previewable_scan()

        self.assertTrue(review_states.preview_only(scan, rows))

    def test_the_rows_are_read_when_the_caller_has_none(self):
        scan, _ = previewable_scan()

        self.assertTrue(review_states.preview_only(scan))

    def test_an_approved_volume_with_no_apply_run_has_one(self):
        """The window between the approval and the apply's first run:
        the disclaimer there says to wait for the corrected volume."""
        scan, rows = previewable_scan(
            status=Status.PAGE_COMPLETENESS_REVIEW_DONE
        )

        self.assertTrue(review_states.preview_only(scan, rows))

    def test_an_apply_in_progress_takes_it_away(self):
        """What the apply builds is the page space the real review
        reads."""
        scan, rows = previewable_scan(
            status=Status.PAGE_COMPLETENESS_REVIEW_DONE
        )
        ApplyRun.objects.create(scan=scan, number=1)

        self.assertFalse(review_states.preview_only(scan, rows))

    def test_a_built_volume_whose_boxes_are_measured_takes_it_away(self):
        """That volume is review 2 itself."""
        scan, rows = merged_scan()
        yolo.record_apply_success(rows, apply.current_run(scan))
        rows = yolo.live_detect_jobs(scan)

        self.assertTrue(review_states.redaction_review_ready(scan, rows))
        self.assertFalse(review_states.preview_only(scan, rows))

    def test_a_complete_run_with_no_measurement_keeps_it(self):
        """The corrected volume is built, and nothing has measured its
        redactions: there is still nothing else to show."""
        scan, rows = previewable_scan(
            status=Status.PAGE_COMPLETENESS_REVIEW_DONE
        )
        glued_run(scan)

        self.assertTrue(review_states.preview_only(scan, rows))

    def test_an_unmerged_run_has_none(self):
        scan, rows = previewable_scan()
        ExternalJob.objects.filter(scan=scan).update(
            status=JobStatus.IN_PROGRESS
        )

        self.assertFalse(
            review_states.preview_only(scan, yolo.live_detect_jobs(scan))
        )

    def test_a_volume_with_no_detection_run_has_none(self):
        scan = ScanFactory(status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW)

        self.assertFalse(review_states.preview_only(scan))

    def test_every_other_status_has_none(self):
        """The set is spelled out (``PREVIEW_STATUSES``): the legacy
        step 2 has its own rows, and a busy or errored volume is
        nobody's to preview."""
        for status in (
            Status.READY_FOR_REDACTION_REVIEW,
            Status.REDACTION_REVIEW_DONE,
            Status.PENDING_REVIEW,
            Status.QUEUED,
            Status.PROCESSING,
            Status.AWAITING,
            Status.AWAITING_VALIDATION,
            Status.ERROR,
            Status.APPROVED,
        ):
            with self.subTest(status=status):
                scan, rows = previewable_scan(status=status)

                self.assertFalse(review_states.preview_only(scan, rows))


class TestThePreviewEntries(TestCase):
    """The merged document, in the shape the viewer draws."""

    def test_the_boxes_come_off_the_merged_document(self):
        scan, rows = previewable_scan()

        with patch.object(
            yolo, "load_merged_document", return_value=merged_document(scan)
        ):
            entries = yolo.preview_entries(scan, rows)

        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["page_index"], 1)
        self.assertEqual(entry["label"], "HEADNOTE")
        self.assertEqual(entry["bbox"], [10, 20, 30, 40])
        self.assertEqual(entry["img_width"], 1700)

    def test_no_box_names_a_row(self):
        """Every per-box control posts the id, and there is no row."""
        scan, rows = previewable_scan()

        with patch.object(
            yolo, "load_merged_document", return_value=merged_document(scan)
        ):
            entries = yolo.preview_entries(scan, rows)

        self.assertIsNone(entries[0]["id"])
        self.assertTrue(entries[0]["preview"])
        self.assertFalse(entries[0]["manual"])
        self.assertIsNone(entries[0]["decision"])

    def test_the_boxes_are_ordered_by_page_and_height(self):
        scan, rows = previewable_scan()
        document = merged_document(
            scan,
            [
                {"page_index": 1, "bbox": [0, 90, 10, 99], "label": "B"},
                {"page_index": 0, "bbox": [0, 10, 10, 19], "label": "A"},
                {"page_index": 1, "bbox": [0, 10, 10, 19], "label": "C"},
            ],
        )

        with patch.object(yolo, "load_merged_document", return_value=document):
            entries = yolo.preview_entries(scan, rows)

        self.assertEqual([e["label"] for e in entries], ["A", "C", "B"])

    def test_a_volume_with_no_run_reads_nothing(self):
        scan = ScanFactory(status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW)

        self.assertEqual(yolo.preview_entries(scan), [])


class TestWhatTheViewerReads(ScanningTestCase):
    """The three JSON endpoints, under a preview."""

    def setUp(self):
        self.user = self.make_user(username="reviewer")
        self.client.force_login(self.user)
        self.scan, self.rows = previewable_scan()

    def _detections(self):
        """Ask the detections endpoint.

        :returns: The parsed answer.
        :rtype: list
        """
        response = self.client.get(
            reverse("serve_detections", kwargs={"pk": self.scan.pk})
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_the_detections_come_from_the_document(self):
        with patch.object(
            yolo,
            "load_merged_document",
            return_value=merged_document(self.scan),
        ):
            data = self._detections()

        self.assertEqual(len(data), 1)
        self.assertIsNone(data[0]["id"])
        self.assertEqual(data[0]["label"], "HEADNOTE")

    def test_the_rows_of_a_superseded_run_are_not_shown(self):
        """A reopen leaves its rows until the next import, measured in
        a page space the preview does not draw."""
        Detection.objects.create(
            scan=self.scan,
            page_index=0,
            label="KEY_ICON",
            label_id=1,
            confidence=1.0,
            x0=1,
            y0=2,
            x1=3,
            y1=4,
        )

        with patch.object(
            yolo,
            "load_merged_document",
            return_value=merged_document(self.scan),
        ):
            data = self._detections()

        self.assertEqual([d["label"] for d in data], ["HEADNOTE"])

    def test_a_document_that_does_not_load_answers_nothing(self):
        """The page around it still renders."""
        with patch.object(
            yolo, "load_merged_document", side_effect=OSError("gone")
        ):
            self.assertEqual(self._detections(), [])

    def test_the_redactions_and_the_opinions_are_empty(self):
        for name in ("serve_redactions", "serve_opinions"):
            with self.subTest(endpoint=name):
                response = self.client.get(
                    reverse(name, kwargs={"pk": self.scan.pk})
                )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), [])

    def test_a_volume_outside_the_preview_still_reads_its_rows(self):
        scan = ScanFactory(status=Status.READY_FOR_REDACTION_REVIEW)
        Detection.objects.create(
            scan=scan,
            page_index=0,
            label="KEY_ICON",
            label_id=1,
            confidence=1.0,
            x0=1,
            y0=2,
            x1=3,
            y1=4,
        )

        response = self.client.get(
            reverse("serve_detections", kwargs={"pk": scan.pk})
        )

        self.assertEqual(len(response.json()), 1)
        self.assertIsNotNone(response.json()[0]["id"])


class TestEveryWriteRefuses(ScanningTestCase):
    """The gate of the preview, in the view and not in the template."""

    #: Every write step 2 can reach, with a body that would otherwise
    #: be taken. The refusal comes before the body is read, so a body
    #: that names no row is enough.
    WRITES = [
        (
            "add_redaction",
            {"page_index": 0, "x0": 1, "y0": 2, "x1": 3, "y1": 4},
        ),
        ("add_single_detection", {"page_index": 0, "label": "HEADNOTE"}),
        ("add_boundary", {"caption_page": 0}),
        ("dismiss_boundary", {"boundary_id": 1}),
        ("restore_boundary", {"boundary_id": 1}),
        ("dismiss_finding", {"issue_id": 1}),
        ("restore_finding", {"issue_id": 1}),
        ("withdraw_stale_edit", {"issue_id": 1}),
        ("rebuild_findings", {}),
        ("delete_detection", {"detection_id": 1}),
        ("update_detection", {"detection_id": 1}),
        ("approve_detection", {"detection_id": 1}),
        ("bake_redactions", {}),
    ]

    def setUp(self):
        self.user = self.make_user(username="reviewer")
        self.client.force_login(self.user)
        self.scan, self.rows = previewable_scan()

    def test_every_write_of_step_two_answers_409(self):
        for name, body in self.WRITES:
            with self.subTest(endpoint=name):
                response = self.client.post(
                    reverse(name, kwargs={"pk": self.scan.pk}),
                    data=json.dumps(body),
                    content_type="application/json",
                )

                self.assertEqual(response.status_code, 409)
                self.assertEqual(
                    response.json()["message"], PREVIEW_READ_ONLY_MESSAGE
                )

    def test_the_per_row_writes_answer_409(self):
        """They 404 today only because no row exists; the gate is what
        makes the rule true whatever the rows are."""
        for name in (
            "move_redaction",
            "dismiss_redaction",
            "restore_redaction",
        ):
            with self.subTest(endpoint=name):
                response = self.client.post(
                    reverse(
                        name, kwargs={"pk": self.scan.pk, "redaction_id": 1}
                    ),
                    data=json.dumps({"x0": 1, "y0": 2, "x1": 3, "y1": 4}),
                    content_type="application/json",
                )

                self.assertEqual(response.status_code, 409)

    def test_no_box_is_written(self):
        from scanning.models import Redaction

        self.client.post(
            reverse("add_redaction", kwargs={"pk": self.scan.pk}),
            data=json.dumps(
                {"page_index": 0, "x0": 1, "y0": 2, "x1": 3, "y1": 4}
            ),
            content_type="application/json",
        )

        self.assertFalse(Redaction.objects.filter(scan=self.scan).exists())

    def test_the_page_edits_of_review_one_still_go_through(self):
        """The preview is step 2's state; the page review is open."""
        response = self.client.post(
            reverse("delete_page", kwargs={"pk": self.scan.pk}),
            data=json.dumps({"pdf_page": 1}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            PageEdit.objects.filter(
                scan=self.scan, kind=PageEdit.Kind.DELETE_PAGE
            ).exists()
        )

    def test_a_volume_outside_the_preview_is_not_gated(self):
        scan = ScanFactory(status=Status.READY_FOR_REDACTION_REVIEW)

        response = self.client.post(
            reverse("rebuild_findings", kwargs={"pk": scan.pk}),
            data="{}",
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)


class TestThePreviewPage(ScanningTestCase):
    """The disclaimer, the locks and the links."""

    def setUp(self):
        self.user = self.make_user(username="reviewer")
        self.client.force_login(self.user)
        self.scan, self.rows = previewable_scan()

    def _page(self, step=2, scan=None):
        """Render the process page.

        :param step: The step to ask for.
        :param scan: The scan; this test's own when omitted.
        :returns: The response.
        """
        response = self.client.get(
            reverse("scan_process", kwargs={"pk": (scan or self.scan).pk}),
            {"step": step},
        )
        self.assertEqual(response.status_code, 200)
        return response

    def _bar(self, step, scan=None):
        """Render the action bar of one step.

        :param step: The step to ask for.
        :param scan: The scan; this test's own when omitted.
        :returns: The rendered HTML.
        :rtype: str
        """
        response = self.client.get(
            reverse("process_actions", kwargs={"pk": (scan or self.scan).pk}),
            {"step": step},
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["html"]

    def test_step_two_says_it_is_a_preview(self):
        response = self._page()

        self.assertTrue(response.context["preview_only"])
        self.assertContains(response, "detection-preview-banner")
        self.assertContains(response, "previewOnly: true")

    def test_an_open_page_review_is_told_to_approve_it(self):
        self.assertContains(
            self._page(), "approve the page\n          completeness review"
        )

    def test_an_approved_volume_is_told_to_wait_for_the_volume(self):
        Scan.objects.filter(pk=self.scan.pk).update(
            status=Status.PAGE_COMPLETENESS_REVIEW_DONE
        )

        self.assertContains(self._page(), "The corrected volume is built next")

    def test_step_one_is_no_preview(self):
        """The same volume's page review is open, and every edit of it
        stands."""
        response = self._page(step=1)

        self.assertFalse(response.context["preview_only"])
        self.assertFalse(response.context["page_edits_locked"])
        self.assertNotContains(response, "detection-preview-banner")

    def test_the_page_numbers_are_locked_in_the_preview(self):
        self.assertTrue(self._page().context["page_edits_locked"])

    def test_no_findings_section(self):
        self.assertNotContains(self._page(), 'id="review-findings"')

    def test_no_boundary_reaches_the_page(self):
        from scanning.factories import OpinionBoundaryFactory

        OpinionBoundaryFactory(scan=self.scan)

        self.assertEqual(self._page().context["opinions"], [])

    def test_a_page_marked_for_deletion_is_shown(self):
        """Nothing is built into the volume until the apply, so the
        page is still in it, and the viewer badges it."""
        self.client.post(
            reverse("delete_page", kwargs={"pk": self.scan.pk}),
            data=json.dumps({"pdf_page": 1}),
            content_type="application/json",
        )

        response = self._page()

        self.assertEqual(
            json.loads(response.context["deleted_pages_json"]), [1]
        )
        self.assertEqual(len(json.loads(response.context["page_map_json"])), 2)

    def test_the_step_two_bar_offers_nothing(self):
        bar = self._bar(2)

        self.assertIn("read only", bar)
        self.assertNotIn("approve-redactions", bar)
        self.assertNotIn("recompute-btn", bar)

    def test_the_step_one_bar_links_the_preview(self):
        self.assertIn("Preview detections", self._bar(1))

    def test_a_volume_with_no_preview_links_none(self):
        scan = ScanFactory(status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW)

        self.assertNotIn("Preview detections", self._bar(1, scan=scan))

    def test_the_guide_names_no_control_the_preview_lacks(self):
        """The detections part and the findings part describe the
        selection, the Draw and the two recomputes."""
        response = self._page()

        self.assertNotContains(response, 'id="viewer-help-detections"')
        self.assertNotContains(response, "Recompute redactions")
        self.assertContains(response, "This volume is a preview")

    def test_the_tab_carries_the_mark_from_step_one(self):
        self.assertContains(self._page(step=1), ">preview</span>")
