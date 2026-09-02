"""Tests for the redaction apply (issue #196).

Two halves, and the split between them is the point of the design:

- :func:`yolo.queue_ready_runs` is the collect tick's trigger. It
  writes one status and returns, because the work renders every page of
  the volume and the tick's scheduler is serial.
- :func:`services.run_compute_redactions` is that work, dispatched by
  ``process_next_scan``. What is tested here is its orchestration --
  what it imports, what it keeps, and where it leaves the scan. The
  geometry itself is blackletter's, and is patched out.
"""

from unittest.mock import patch

from django.test import TestCase

from scanning import services, yolo
from scanning.factories import ScanFactory
from scanning.models import (
    Detection,
    ExternalJob,
    JobStatus,
    QueuedAction,
    Scan,
    Status,
)
from scanning.tests.test_jobs import make_manifest

#: One merged document, as :func:`yolo.merge_detect_results` writes it.
DOCUMENT = {
    "schema_version": 1,
    "engine": "blackletter",
    "action": "detect",
    "run": 1,
    "source_page_count": 2,
    "source_fingerprint": "",
    "dpi": 200,
    "models": ["bl_warm"],
    "detections": [
        {
            "page_index": 0,
            "pdf_page": 1,
            "shard_index": 0,
            "label": "PAGE_HEADER",
            "label_id": 2,
            "confidence": 0.92,
            "bbox": [10.0, 20.0, 30.0, 40.0],
            "img_width": 1700,
            "img_height": 2200,
            "found_by": [{"model": "bl_warm", "confidence": 0.92}],
            "model_count": 1,
        },
        {
            "page_index": 1,
            "pdf_page": 2,
            "shard_index": 1,
            "label": "CASE_CAPTION",
            "label_id": 3,
            "confidence": 0.81,
            "bbox": [11.0, 21.0, 31.0, 41.0],
            "img_width": 1700,
            "img_height": 2200,
            "found_by": [{"model": "bl_warm", "confidence": 0.81}],
            "model_count": 1,
        },
    ],
    "pages_with_detections": 2,
}


def merged_scan(status=Status.PAGE_COMPLETENESS_REVIEW_DONE, **kwargs):
    """Build a scan whose detection run is merged and consumed.

    :param status: The status to give the scan.
    :param kwargs: Extra fields for the factory.
    :returns: ``(scan, rows)``.
    """
    scan = ScanFactory(page_count=2, status=status, **kwargs)
    yolo.ensure_detect_jobs(scan, make_manifest(2, 1))
    ExternalJob.objects.filter(scan=scan).update(status=JobStatus.CONSUMED)
    return scan, yolo.live_detect_jobs(scan)


class TestQueueReadyRuns(TestCase):
    """The trigger on the collect tick."""

    def setUp(self):
        active = patch("scanning.s3_sync.s3_active", return_value=True)
        active.start()
        self.addCleanup(active.stop)

    def test_an_approved_volume_is_queued(self):
        scan, rows = merged_scan()

        self.assertEqual(yolo.queue_ready_runs(), 1)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.QUEUED)
        self.assertEqual(scan.queued_action, QueuedAction.COMPUTE_REDACTIONS)
        # Re-read: the pass wrote to rows of its own.
        self.assertTrue(
            yolo.apply_state(yolo.live_detect_jobs(scan)).get("queued_at")
        )

    def test_the_apply_itself_does_not_run_here(self):
        """It renders every page; the tick's scheduler is serial."""
        merged_scan()

        with patch.object(services, "run_compute_redactions") as run:
            yolo.queue_ready_runs()

        run.assert_not_called()

    def test_a_queued_scan_is_not_queued_twice(self):
        merged_scan()

        self.assertEqual(yolo.queue_ready_runs(), 1)
        self.assertEqual(yolo.queue_ready_runs(), 0)

    def test_a_scan_that_lost_its_claim_is_queued_again(self):
        """An admin re-queue takes that path. The stamp is an audit
        trail, not a guard, or the volume would sit approved for ever
        without its geometry."""
        scan, _ = merged_scan()
        yolo.queue_ready_runs()
        Scan.objects.filter(pk=scan.pk).update(
            status=Status.PAGE_COMPLETENESS_REVIEW_DONE
        )

        self.assertEqual(yolo.queue_ready_runs(), 1)

    def test_a_volume_review_one_has_not_approved_is_deferred(self):
        """Deferred, not marked: it comes back when it is approved."""
        scan, rows = merged_scan(
            status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )

        self.assertEqual(yolo.queue_ready_runs(), 0)

        scan.refresh_from_db()
        self.assertEqual(
            scan.status, Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )
        self.assertEqual(yolo.apply_state(yolo.live_detect_jobs(scan)), {})

    def test_an_applied_run_is_never_queued_again(self):
        _, rows = merged_scan()
        yolo.record_apply_success(rows)

        self.assertEqual(yolo.queue_ready_runs(), 0)

    def test_a_run_out_of_attempts_is_left_alone(self):
        scan, rows = merged_scan()
        yolo.write_apply_state(rows, {"attempts": yolo.APPLY_MAX_ATTEMPTS})

        self.assertEqual(yolo.queue_ready_runs(), 0)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.PAGE_COMPLETENESS_REVIEW_DONE)

    def test_a_run_that_is_not_merged_yet_is_left_alone(self):
        scan, rows = merged_scan()
        ExternalJob.objects.filter(pk=rows[1].pk).update(
            status=JobStatus.COMPLETED
        )

        self.assertEqual(yolo.queue_ready_runs(), 0)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.PAGE_COMPLETENESS_REVIEW_DONE)

    def test_nothing_happens_while_s3_is_off(self):
        merged_scan()

        with patch("scanning.s3_sync.s3_active", return_value=False):
            self.assertEqual(yolo.queue_ready_runs(), 0)


class ComputeMixin:
    """Patches the geometry, which belongs to blackletter."""

    def patch_geometry(self, document=None):
        """Stub every step that needs a PDF, and return the stubs.

        :param document: The merged document to answer with.
        :returns: A dict of the patched callables.
        """
        # The daemon entry point closes its connections, which would
        # tear down the test transaction.
        closed = patch("django.db.connections.close_all")
        closed.start()
        self.addCleanup(closed.stop)
        stubs = {}
        for name, value in (
            ("_pull_processing_files_from_s3", None),
            ("_push_processing_files_to_s3", True),
            ("_snap_text_columns_to_ink", 0),
            ("_sync_detections_to_disk", []),
            ("_compute_and_save_redaction_rects", []),
            ("_compute_and_save_margin_rects", []),
        ):
            patcher = patch.object(
                services, name, return_value=value, autospec=True
            )
            stubs[name] = patcher.start()
            self.addCleanup(patcher.stop)
        pdf = patch.object(
            services, "processing_pdf_path", return_value="/tmp/x.pdf"
        )
        stubs["processing_pdf_path"] = pdf.start()
        self.addCleanup(pdf.stop)
        pair = patch.object(services, "bl_pair", return_value=[{"a": 1}])
        stubs["bl_pair"] = pair.start()
        self.addCleanup(pair.stop)
        load = patch.object(
            yolo, "load_merged_document", return_value=document or DOCUMENT
        )
        stubs["load_merged_document"] = load.start()
        self.addCleanup(load.stop)
        release = patch("scanning.s3_sync.release_local_processing")
        release.start()
        self.addCleanup(release.stop)
        return stubs


class TestRunComputeRedactions(ComputeMixin, TestCase):
    """The queued work itself."""

    def test_the_detections_are_imported_with_their_provenance(self):
        scan, rows = merged_scan()
        self.patch_geometry()

        services.run_compute_redactions(scan.pk)

        saved = list(
            Detection.objects.filter(scan=scan).order_by("page_index")
        )
        self.assertEqual([d.page_index for d in saved], [0, 1])
        self.assertEqual(
            [d.label for d in saved], ["PAGE_HEADER", "CASE_CAPTION"]
        )
        self.assertEqual([d.x0 for d in saved], [10.0, 11.0])
        # The model family, which picks the confidence gates.
        self.assertEqual(
            {d.model_name for d in saved},
            {Detection.ModelName.BL_WARM},
        )
        self.assertEqual(
            saved[0].found_by, [{"model": "bl_warm", "confidence": 0.92}]
        )

    def test_the_scan_is_handed_back_to_review_one_done(self):
        scan, rows = merged_scan()
        self.patch_geometry()

        services.run_compute_redactions(scan.pk)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.PAGE_COMPLETENESS_REVIEW_DONE)
        state = yolo.apply_state(yolo.live_detect_jobs(scan))
        self.assertTrue(state.get("applied_at"))
        self.assertNotIn("queued_at", state)

    def test_the_opinions_and_the_geometry_are_written(self):
        scan, _ = merged_scan()
        stubs = self.patch_geometry()

        services.run_compute_redactions(scan.pk)

        scan.refresh_from_db()
        self.assertEqual(scan.opinions_json, [{"a": 1}])
        stubs["_compute_and_save_redaction_rects"].assert_called_once()
        # force=True: the strips are measured from the detections, so a
        # fresh run must replace the stored ones.
        self.assertTrue(
            stubs["_compute_and_save_margin_rects"].call_args.kwargs["force"]
        )

    def test_a_hand_made_detection_survives_the_import(self):
        """A curator's box costs curator time, and it addresses the
        same physical page."""
        scan, _ = merged_scan()
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
            model_name=Detection.ModelName.MANUAL,
        )
        stale = Detection.objects.create(
            scan=scan,
            page_index=1,
            label="DIVIDER",
            label_id=1,
            confidence=0.5,
            x0=1,
            y0=2,
            x1=3,
            y1=4,
            model_name=Detection.ModelName.LARGE,
        )
        self.patch_geometry()

        services.run_compute_redactions(scan.pk)

        self.assertTrue(
            Detection.objects.filter(
                scan=scan, model_name=Detection.ModelName.MANUAL
            ).exists()
        )
        self.assertFalse(Detection.objects.filter(pk=stale.pk).exists())

    def test_a_recompute_keeps_the_rows_a_curator_edited(self):
        """A run already applied is a recompute: measure again, import
        nothing."""
        scan, rows = merged_scan()
        self.patch_geometry()
        services.run_compute_redactions(scan.pk)
        Detection.objects.filter(scan=scan).update(active=False)

        services.run_compute_redactions(scan.pk)

        self.assertEqual(
            Detection.objects.filter(scan=scan, active=True).count(), 0
        )

    def test_a_recompute_does_not_re_read_the_document(self):
        scan, _ = merged_scan()
        stubs = self.patch_geometry()
        services.run_compute_redactions(scan.pk)
        stubs["load_merged_document"].reset_mock()

        services.run_compute_redactions(scan.pk)

        stubs["load_merged_document"].assert_not_called()

    def test_a_legacy_volume_measures_what_the_database_holds(self):
        """No detect rows at all: the old pipeline wrote its
        detections, and a curator may still recompute their geometry."""
        scan = ScanFactory(
            page_count=2, status=Status.PAGE_COMPLETENESS_REVIEW_DONE
        )
        Detection.objects.create(
            scan=scan,
            page_index=0,
            label="KEY_ICON",
            label_id=1,
            confidence=0.9,
            x0=1,
            y0=2,
            x1=3,
            y1=4,
            model_name=Detection.ModelName.LARGE,
        )
        stubs = self.patch_geometry()

        services.run_compute_redactions(scan.pk)

        stubs["load_merged_document"].assert_not_called()
        stubs["_compute_and_save_redaction_rects"].assert_called_once()
        self.assertEqual(Detection.objects.filter(scan=scan).count(), 1)

    def test_a_legacy_volume_goes_back_to_pending_review(self):
        """The #154 states describe a review it never had, and its own
        step 2 lives in PENDING_REVIEW."""
        scan = ScanFactory(page_count=2, status=Status.PENDING_REVIEW)
        Detection.objects.create(
            scan=scan,
            page_index=0,
            label="KEY_ICON",
            label_id=1,
            confidence=0.9,
            x0=1,
            y0=2,
            x1=3,
            y1=4,
            model_name=Detection.ModelName.LARGE,
        )
        Scan.objects.filter(pk=scan.pk).update(status=Status.PROCESSING)
        self.patch_geometry()

        services.run_compute_redactions(scan.pk)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.PENDING_REVIEW)

    def test_a_scan_with_nothing_to_measure_is_parked(self):
        scan = ScanFactory(
            page_count=2, status=Status.PAGE_COMPLETENESS_REVIEW_DONE
        )
        Scan.objects.filter(pk=scan.pk).update(status=Status.PROCESSING)
        self.patch_geometry()

        with self.assertLogs("scanning.services", level="WARNING"):
            services.run_compute_redactions(scan.pk)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.PAGE_COMPLETENESS_REVIEW_DONE)

    def test_a_failure_never_errors_an_approved_volume(self):
        """An ERROR status would need an admin re-queue, and that
        re-queue runs the whole pipeline again."""
        scan, rows = merged_scan()
        stubs = self.patch_geometry()
        stubs["_compute_and_save_redaction_rects"].side_effect = RuntimeError(
            "boom"
        )

        with self.assertLogs("scanning", level="WARNING"):
            services.run_compute_redactions(scan.pk)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.PAGE_COMPLETENESS_REVIEW_DONE)
        state = yolo.apply_state(yolo.live_detect_jobs(scan))
        self.assertEqual(state["attempts"], 1)
        self.assertIn("boom", state["last_error"])
        self.assertNotIn("applied_at", state)

    def test_a_failed_apply_is_queued_again(self):
        scan, rows = merged_scan()
        stubs = self.patch_geometry()
        stubs["bl_pair"].side_effect = RuntimeError("boom")
        with self.assertLogs("scanning", level="WARNING"):
            services.run_compute_redactions(scan.pk)

        with patch("scanning.s3_sync.s3_active", return_value=True):
            self.assertEqual(yolo.queue_ready_runs(), 1)

    def test_a_document_from_another_original_is_refused(self):
        scan, rows = merged_scan()
        scan.source_fingerprint = "today"
        scan.save(update_fields=["source_fingerprint"])
        self.patch_geometry()
        # The real reader, against a document from other bytes.
        patcher = patch.object(
            yolo,
            "load_merged_document",
            side_effect=yolo.DetectMergeError("another original"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        with self.assertLogs("scanning", level="WARNING"):
            services.run_compute_redactions(scan.pk)

        self.assertEqual(Detection.objects.filter(scan=scan).count(), 0)
        self.assertIn(
            "another original",
            yolo.apply_state(yolo.live_detect_jobs(scan))["last_error"],
        )


class TestQueueRedactionCompute(TestCase):
    """The helper the two review-2 buttons call."""

    def test_an_approved_volume_is_queued(self):
        scan = ScanFactory(status=Status.PAGE_COMPLETENESS_REVIEW_DONE)

        queued, message = services.queue_redaction_compute(scan)

        self.assertTrue(queued)
        self.assertIn("Queued", message)
        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.QUEUED)
        self.assertEqual(scan.queued_action, QueuedAction.COMPUTE_REDACTIONS)

    def test_a_legacy_volume_is_queued_too(self):
        scan = ScanFactory(status=Status.PENDING_REVIEW)

        queued, _ = services.queue_redaction_compute(scan)

        self.assertTrue(queued)

    def test_a_busy_volume_is_refused(self):
        scan = ScanFactory(status=Status.AWAITING)

        queued, message = services.queue_redaction_compute(scan)

        self.assertFalse(queued)
        self.assertIn("busy", message)
        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.AWAITING)

    def test_a_second_press_is_refused(self):
        scan = ScanFactory(status=Status.PAGE_COMPLETENESS_REVIEW_DONE)

        services.queue_redaction_compute(scan)
        scan.refresh_from_db()
        queued, _ = services.queue_redaction_compute(scan)

        self.assertFalse(queued)
