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

from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from scanning import apply, boundaries, detections, redactions, services, yolo
from scanning.factories import ScanFactory
from scanning.models import (
    ApplyRun,
    Detection,
    DetectionDecision,
    ExternalJob,
    JobStatus,
    QueuedAction,
    Redaction,
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

#: One printed-page map, as :func:`apply.printed_pages` writes it.
PRINTED = {
    "schema_version": 1,
    "apply_run": "a1",
    "final_page_count": 2,
    "pages": [
        {"final_page": 1, "printed": "101", "type": "single", "by": "model"},
        {
            "final_page": 2,
            "printed": "102-103",
            "type": "range",
            "by": "curator",
        },
    ],
}


def identity_map(pages: int) -> dict:
    """Return a stored page map that keeps every original page in place.

    :param pages: The page count.
    :returns: The map, in the shape of ``ApplyPlan.to_map``.
    """
    return {
        "schema_version": apply.MAP_SCHEMA_VERSION,
        "source_page_count": pages,
        "final_page_count": pages,
        "deleted_pages": [],
        "pages": [
            {
                "final_page": page,
                "source": {"kind": "original", "pdf_page": page},
            }
            for page in range(1, pages + 1)
        ],
    }


def glued_run(scan, number: int = 1, page_map: dict | None = None) -> ApplyRun:
    """Create a complete apply run for ``scan``.

    :param scan: The scan.
    :param number: The run number.
    :param page_map: The stored map; the identity over the scan's pages
        when omitted, as a volume with no page edit has.
    :returns: The run.
    """
    return ApplyRun.objects.create(
        scan=scan,
        number=number,
        built_at=timezone.now(),
        page_map=page_map or identity_map(scan.page_count or 2),
        final_pdf_key=f"processing/{scan.pk}/final-{number}.pdf",
        bitonal_key=f"processing/{scan.pk}/bitonal-{number}.pdf",
        ocr_key=f"processing/{scan.pk}/ocr-{number}.json",
        printed_pages_key=f"processing/{scan.pk}/printed-{number}.json",
        detections_key=f"processing/{scan.pk}/detections-{number}.json",
    )


def merged_scan(status=Status.PAGE_COMPLETENESS_REVIEW_DONE, **kwargs):
    """Build a scan whose detection run is merged and consumed.

    :param status: The status to give the scan.
    :param kwargs: Extra fields for the factory.
    :returns: ``(scan, rows)``.
    """
    scan = ScanFactory(page_count=2, status=status, **kwargs)
    yolo.ensure_detect_jobs(scan, make_manifest(2, 1))
    ExternalJob.objects.filter(scan=scan).update(status=JobStatus.CONSUMED)
    # The redaction compute reads the final volume (#224), so the
    # trigger waits for a glued apply run. This one aliases the
    # review-1 artifacts, as a volume with no page edit does.
    glued_run(scan)
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
        scan, rows = merged_scan()
        yolo.record_apply_success(rows, apply.current_run(scan))

        self.assertEqual(yolo.queue_ready_runs(), 0)

    def test_a_run_applied_against_a_superseded_run_is_queued_again(self):
        """The rows describe the pages of ``a1``; ``a2`` may show other
        pages (#269). The ledger starts over, so the failures of the
        new run count against it and the cap still holds."""
        scan, rows = merged_scan()
        old = apply.current_run(scan)
        yolo.record_apply_success(rows, old)
        yolo.write_apply_state(
            yolo.live_detect_jobs(scan),
            {
                **yolo.apply_state(yolo.live_detect_jobs(scan)),
                "attempts": yolo.APPLY_MAX_ATTEMPTS,
            },
        )
        apply.supersede_runs(scan, "test")
        new = glued_run(scan, number=2)

        self.assertEqual(yolo.queue_ready_runs(), 1)

        state = yolo.apply_state(yolo.live_detect_jobs(scan))
        self.assertEqual(state.get("apply_run"), new.pk)
        self.assertIsNone(state.get("applied_at"))
        self.assertEqual(int(state.get("attempts") or 0), 0)

    def test_failures_under_the_new_run_still_reach_the_cap(self):
        """The ledger is written over, not reset: three failures under
        ``a2`` stop the queue, or it would re-queue every tick."""
        scan, rows = merged_scan()
        yolo.record_apply_success(rows, apply.current_run(scan))
        apply.supersede_runs(scan, "test")
        glued_run(scan, number=2)
        self.assertEqual(yolo.queue_ready_runs(), 1)
        Scan.objects.filter(pk=scan.pk).update(
            status=Status.PAGE_COMPLETENESS_REVIEW_DONE
        )
        for _ in range(yolo.APPLY_MAX_ATTEMPTS):
            yolo.record_apply_failure(
                scan, yolo.live_detect_jobs(scan), RuntimeError("no")
            )

        self.assertEqual(yolo.queue_ready_runs(), 0)

    def test_a_stamp_with_no_run_is_not_current(self):
        """A stamp written before #269 names no run; the migration
        stamps the identity runs, and every other one is measured
        again."""
        scan, rows = merged_scan()
        yolo.write_apply_state(rows, {"applied_at": "2026-09-01T00:00:00"})

        self.assertFalse(
            yolo.redactions_current(
                yolo.live_detect_jobs(scan), apply.current_run(scan)
            )
        )
        self.assertEqual(yolo.queue_ready_runs(), 1)

    def test_a_volume_whose_corrected_build_is_missing_waits(self):
        scan, _ = merged_scan()
        ApplyRun.objects.filter(scan=scan).update(detections_key="")

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
            ("detection_entries", []),
            ("_measure_redaction_rects", []),
            ("_measure_margin_rects", []),
        ):
            patcher = patch.object(
                services, name, return_value=value, autospec=True
            )
            stubs[name] = patcher.start()
            self.addCleanup(patcher.stop)
        pdf = patch.object(
            services, "geometry_pdf_path", return_value="/tmp/x.pdf"
        )
        stubs["geometry_pdf_path"] = pdf.start()
        self.addCleanup(pdf.stop)
        # The pairing writes the boundary rows (#240 PR C); the
        # document it reads needs a PDF, which the stubbed
        # ``detection_entries`` leaves empty, so stub the write.
        pair = patch.object(boundaries, "write_computed", return_value=[])
        stubs["write_computed"] = pair.start()
        self.addCleanup(pair.stop)
        # The snapped document the pairing and the geometry read; the
        # stubbed ``detection_entries`` would leave it with no pages, and
        # the redaction rows need the page scales (#240 PR B).
        snapped = patch.object(
            services,
            "_snapped_document",
            return_value=(SimpleNamespace(pages=[]), {}, []),
        )
        stubs["_snapped_document"] = snapped.start()
        self.addCleanup(snapped.stop)
        stamp = patch.object(boundaries, "stamp_uncovered", return_value=0)
        stubs["stamp_uncovered"] = stamp.start()
        self.addCleanup(pair.stop)
        # The compute reads the run's two documents (#269): the glued
        # detections in the final page space, and the printed pages.
        load = patch.object(
            apply,
            "load_detections_document",
            return_value=document or DOCUMENT,
        )
        stubs["load_detections_document"] = load.start()
        self.addCleanup(load.stop)
        printed = patch.object(
            apply, "load_printed_pages", return_value=PRINTED
        )
        stubs["load_printed_pages"] = printed.start()
        self.addCleanup(printed.stop)
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
        # One pairing, in the run's space, from the merged detection
        # run; the rects are measured from the same pairs.
        stubs["write_computed"].assert_called_once()
        args = stubs["write_computed"].call_args.args
        self.assertEqual(args[0].pk, scan.pk)
        self.assertEqual(args[3], apply.current_run(scan))
        self.assertEqual(args[4], 1)
        stubs["_measure_redaction_rects"].assert_called_once()
        self.assertEqual(
            stubs["_measure_redaction_rects"].call_args.args[1], []
        )
        stubs["_measure_margin_rects"].assert_called_once()

    def _measured(self, stubs, rects_px, margins_pt=None):
        """Make the geometry stubs answer one page of boxes.

        The fake document has two pages with a scale of 0.5 point per
        pixel, so a pixel box of 100 reads as 50 points.

        :param stubs: The patched callables.
        :param rects_px: blackletter's redaction rects, in pixels.
        :param margins_pt: blackletter's strips, in points.
        """
        pages = [
            SimpleNamespace(index=0, scale_x=0.5, scale_y=0.5),
            SimpleNamespace(index=1, scale_x=0.5, scale_y=0.5),
        ]
        stubs["_snapped_document"].return_value = (
            SimpleNamespace(pages=pages),
            {},
            [{"page_index": 0}],
        )
        stubs["_measure_redaction_rects"].return_value = rects_px
        stubs["_measure_margin_rects"].return_value = margins_pt or []

    def test_the_compute_writes_the_rows_in_points(self):
        """One computed row per rect and per strip, addressed, in the
        run's space, in points (#240 PR B); nothing on the scan."""
        scan, rows = merged_scan()
        stubs = self.patch_geometry()
        self._measured(
            stubs,
            [
                {
                    "page_index": 0,
                    "rects": [
                        {
                            "x0": 100,
                            "y0": 200,
                            "x1": 300,
                            "y1": 400,
                            "fill": "black",
                            "type": "headnote",
                        }
                    ],
                }
            ],
            [
                {
                    "page_index": 1,
                    "rects": [{"x0": 0, "y0": 0, "x1": 20, "y1": 50}],
                }
            ],
        )

        services.run_compute_redactions(scan.pk)

        run = apply.current_run(scan)
        written = list(
            Redaction.objects.filter(scan=scan).order_by("page_index")
        )
        self.assertEqual(len(written), 2)
        head, margin = written
        self.assertEqual(head.origin, Redaction.Origin.COMPUTED)
        self.assertEqual(head.rect_type, "headnote")
        self.assertEqual(head.bbox, [50.0, 100.0, 150.0, 200.0])
        self.assertEqual((head.source_page, head.page_index), (1, 0))
        self.assertEqual(head.apply_run, run)
        self.assertEqual(head.detect_run, rows[0].run)
        self.assertEqual(margin.rect_type, "margin")
        self.assertEqual(margin.fill, "white")
        self.assertEqual(margin.bbox, [0.0, 0.0, 20.0, 50.0])
        self.assertEqual((margin.source_page, margin.page_index), (2, 1))

    def test_a_recompute_rewrites_the_computed_rows_and_keeps_the_human_ones(
        self,
    ):
        scan, _ = merged_scan()
        stubs = self.patch_geometry()
        rect = {
            "x0": 100,
            "y0": 200,
            "x1": 300,
            "y1": 400,
            "fill": "black",
            "type": "headnote",
        }
        self._measured(stubs, [{"page_index": 0, "rects": [rect]}])
        services.run_compute_redactions(scan.pk)
        first = Redaction.objects.get(scan=scan)
        drawn = redactions.add(scan, 0, [1.0, 2.0, 3.0, 4.0], "white", None)
        dismissed = redactions.dismiss(scan, first, None)

        services.run_compute_redactions(scan.pk)

        self.assertFalse(Redaction.objects.filter(pk=first.pk).exists())
        second = Redaction.objects.computed().get(scan=scan)
        self.assertEqual(second.decision, dismissed)
        drawn.refresh_from_db()
        self.assertIsNone(drawn.withdrawn_at)
        self.assertEqual(
            [
                r["id"]
                for e in redactions.visible_by_page(scan)
                for r in e["rects"]
            ],
            [drawn.pk],
        )

    def test_a_drawn_box_follows_the_page_space_of_the_new_run(self):
        """Drawn on final page 2 under ``a1`` (original page 2); ``a2``
        deletes page 1, and the box is on final page 1."""
        scan, _ = merged_scan()
        stubs = self.patch_geometry()
        self._measured(stubs, [])
        services.run_compute_redactions(scan.pk)
        drawn = redactions.add(scan, 1, [1.0, 2.0, 3.0, 4.0], "black", None)
        apply.supersede_runs(scan, "test")
        new = glued_run(
            scan,
            number=2,
            page_map={
                **identity_map(2),
                "final_page_count": 1,
                "deleted_pages": [1],
                "pages": [
                    {
                        "final_page": 1,
                        "source": {"kind": "original", "pdf_page": 2},
                    }
                ],
            },
        )

        services.run_compute_redactions(scan.pk)

        drawn.refresh_from_db()
        self.assertEqual(drawn.page_index, 0)
        self.assertEqual(drawn.apply_run, new)
        self.assertEqual(drawn.source_page, 2)

    def test_the_uncovered_headnotes_are_stamped_on_the_boundaries(self):
        """A confident HEADNOTE box no headnote rect covers is measured
        in pixels, before the rows are converted, and handed to the
        boundaries (#240 PR B)."""
        scan, _ = merged_scan()
        stubs = self.patch_geometry()
        stubs["load_detections_document"].return_value = {
            **DOCUMENT,
            "detections": [
                {
                    **DOCUMENT["detections"][0],
                    "label": "HEADNOTE",
                    "label_id": 5,
                    "confidence": 0.95,
                    "bbox": [100.0, 100.0, 200.0, 200.0],
                }
            ],
        }
        self._measured(stubs, [{"page_index": 0, "rects": []}])

        services.run_compute_redactions(scan.pk)

        stubs["stamp_uncovered"].assert_called_once()
        self.assertEqual(stubs["stamp_uncovered"].call_args.args[1], {0})

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
        stubs["load_detections_document"].reset_mock()

        services.run_compute_redactions(scan.pk)

        stubs["load_detections_document"].assert_not_called()

    def test_the_geometry_is_measured_on_the_run_and_labelled_by_it(self):
        """The compute reads the standing run's corrected volume
        (#269): the PDF it measures is the run's bitonal copy, and the
        page numbers written beside each box come from the run's
        printed pages, in both reads of the detection entries."""
        scan, _ = merged_scan()
        stubs = self.patch_geometry()

        services.run_compute_redactions(scan.pk)

        run = apply.current_run(scan)
        stubs["geometry_pdf_path"].assert_called_once()
        self.assertEqual(
            stubs["geometry_pdf_path"].call_args.args[1].pk, run.pk
        )
        stubs["load_printed_pages"].assert_called_once()
        expected = {0: (101, None), 1: (102, 103)}
        # The snapped document is built with the run's numbers, and the
        # measurers read it (#240 PR B).
        self.assertEqual(
            stubs["_snapped_document"].call_args.args[2], expected
        )
        # The whole-prefix pull lands the multi-GB original; one key
        # is enough.
        stubs["_pull_processing_files_from_s3"].assert_not_called()
        state = yolo.apply_state(yolo.live_detect_jobs(scan))
        self.assertEqual(state.get("apply_run"), run.pk)

    def test_a_new_apply_run_imports_the_detections_again(self):
        """Rows measured against ``a1`` describe pages ``a2`` may not
        show. A write on the rows themselves does not survive it; a
        decision does (``test_a_decision_survives_the_re_import``)."""
        scan, _ = merged_scan()
        stubs = self.patch_geometry()
        services.run_compute_redactions(scan.pk)
        Detection.objects.filter(scan=scan).update(active=False)
        apply.supersede_runs(scan, "test")
        new = glued_run(scan, number=2)
        stubs["load_detections_document"].reset_mock()

        services.run_compute_redactions(scan.pk)

        stubs["load_detections_document"].assert_called_once()
        self.assertEqual(
            Detection.objects.filter(scan=scan, active=True).count(), 2
        )
        state = yolo.apply_state(yolo.live_detect_jobs(scan))
        self.assertEqual(state.get("apply_run"), new.pk)

    def test_the_import_stamps_the_address_and_the_runs(self):
        """Every row names its source page, the apply run whose space
        ``page_index`` is in, and the detection run (#240)."""
        scan, rows = merged_scan()
        self.patch_geometry()

        services.run_compute_redactions(scan.pk)

        run = apply.current_run(scan)
        imported = list(
            Detection.objects.filter(scan=scan).order_by("page_index")
        )
        self.assertEqual([d.source_page for d in imported], [1, 2])
        self.assertEqual({d.source_edit for d in imported}, {None})
        self.assertEqual({d.apply_run for d in imported}, {run})
        self.assertEqual({d.detect_run for d in imported}, {rows[0].run})
        self.assertEqual(
            {d.source_fingerprint for d in imported}, {scan.source_fingerprint}
        )

    def test_a_hand_drawn_box_follows_the_new_page_space(self):
        """Drawn on final page 2 under ``a1`` (original page 2); ``a2``
        deletes page 1, and the box is on final page 1 (PR #288 review).
        A box on the deleted page is left where it was and logged."""
        scan, _ = merged_scan()
        stubs = self.patch_geometry()
        services.run_compute_redactions(scan.pk)
        kept = detections.add_manual(
            scan, 1, "KEY_ICON", 1, [1.0, 2.0, 3.0, 4.0], 1700, 2200
        )
        gone = detections.add_manual(
            scan, 0, "KEY_ICON", 1, [1.0, 2.0, 3.0, 4.0], 1700, 2200
        )
        apply.supersede_runs(scan, "test")
        new = glued_run(
            scan,
            number=2,
            page_map={
                **identity_map(2),
                "final_page_count": 1,
                "deleted_pages": [1],
                "pages": [
                    {
                        "final_page": 1,
                        "source": {"kind": "original", "pdf_page": 2},
                    }
                ],
            },
        )
        stubs["load_detections_document"].reset_mock()

        with self.assertLogs("scanning.detections", level="WARNING"):
            services.run_compute_redactions(scan.pk)

        kept.refresh_from_db()
        gone.refresh_from_db()
        self.assertEqual(kept.page_index, 0)
        self.assertEqual(kept.apply_run, new)
        self.assertEqual(kept.source_page, 2)
        self.assertEqual(gone.page_index, 0)
        self.assertTrue(gone.active)

    def test_a_decision_survives_the_re_import(self):
        """The curator deleted one box and approved another under ``a1``;
        ``a2`` imports the run again, and both decisions land on the new
        rows with the same box (#240)."""
        scan, _ = merged_scan()
        stubs = self.patch_geometry()
        services.run_compute_redactions(scan.pk)
        header = Detection.objects.get(scan=scan, label="PAGE_HEADER")
        caption = Detection.objects.get(scan=scan, label="CASE_CAPTION")
        detections.decide(
            scan, header, DetectionDecision.Kind.DEACTIVATE, None
        )
        detections.decide(scan, caption, DetectionDecision.Kind.APPROVE, None)
        apply.supersede_runs(scan, "test")
        glued_run(scan, number=2)
        stubs["load_detections_document"].reset_mock()

        services.run_compute_redactions(scan.pk)

        stubs["load_detections_document"].assert_called_once()
        self.assertFalse(Detection.objects.filter(pk=header.pk).exists())
        new_header = Detection.objects.get(scan=scan, label="PAGE_HEADER")
        new_caption = Detection.objects.get(scan=scan, label="CASE_CAPTION")
        self.assertFalse(new_header.active)
        self.assertEqual(
            new_header.decision.kind, DetectionDecision.Kind.DEACTIVATE
        )
        self.assertEqual(new_caption.confidence, 1.0)
        self.assertEqual(
            new_caption.decision.kind, DetectionDecision.Kind.APPROVE
        )
        self.assertEqual(
            DetectionDecision.objects.filter(
                scan=scan, withdrawn_at__isnull=True
            ).count(),
            2,
        )

    def test_a_decision_on_a_box_that_moved_is_left_standing_and_logged(self):
        """A model that draws the box elsewhere is another finding: the
        decision lands on nothing, and it is not deleted."""
        scan, _ = merged_scan()
        stubs = self.patch_geometry()
        services.run_compute_redactions(scan.pk)
        header = Detection.objects.get(scan=scan, label="PAGE_HEADER")
        decision = detections.decide(
            scan, header, DetectionDecision.Kind.DEACTIVATE, None
        )
        moved = {
            **DOCUMENT,
            "detections": [
                {
                    **DOCUMENT["detections"][0],
                    "bbox": [500.0, 500.0, 520.0, 520.0],
                },
                DOCUMENT["detections"][1],
            ],
        }
        stubs["load_detections_document"].return_value = moved
        apply.supersede_runs(scan, "test")
        glued_run(scan, number=2)

        with self.assertLogs("scanning.detections", level="WARNING") as logs:
            services.run_compute_redactions(scan.pk)

        new_header = Detection.objects.get(scan=scan, label="PAGE_HEADER")
        self.assertTrue(new_header.active)
        self.assertIsNone(new_header.decision)
        decision.refresh_from_db()
        self.assertIsNone(decision.withdrawn_at)
        self.assertIn(f"#{decision.pk}", logs.output[0])

    def test_a_decision_of_another_original_lands_on_nothing(self):
        scan, _ = merged_scan()
        Scan.objects.filter(pk=scan.pk).update(source_fingerprint="10:2")
        stubs = self.patch_geometry()
        services.run_compute_redactions(scan.pk)
        header = Detection.objects.get(scan=scan, label="PAGE_HEADER")
        decision = detections.decide(
            scan, header, DetectionDecision.Kind.DEACTIVATE, None
        )
        DetectionDecision.objects.filter(pk=decision.pk).update(
            source_fingerprint="999:2"
        )
        apply.supersede_runs(scan, "test")
        glued_run(scan, number=2)
        stubs["load_detections_document"].reset_mock()

        with self.assertLogs("scanning.detections", level="WARNING"):
            services.run_compute_redactions(scan.pk)

        self.assertTrue(
            Detection.objects.get(scan=scan, label="PAGE_HEADER").active
        )

    def test_no_corrected_volume_parks_the_scan_and_spends_no_attempt(self):
        """The queue gate checked it; this is the backstop for an admin
        supersede between the queue and the claim."""
        scan, rows = merged_scan(status=Status.PROCESSING)
        stubs = self.patch_geometry()
        apply.supersede_runs(scan, "test")

        with self.assertLogs("scanning.services", level="WARNING"):
            services.run_compute_redactions(scan.pk)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.PAGE_COMPLETENESS_REVIEW_DONE)
        self.assertIn("not built yet", scan.progress_message)
        stubs["load_detections_document"].assert_not_called()
        state = yolo.apply_state(yolo.live_detect_jobs(scan))
        self.assertEqual(int(state.get("attempts") or 0), 0)
        self.assertNotIn("queued_at", state)

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

        stubs["load_detections_document"].assert_not_called()
        stubs["_measure_redaction_rects"].assert_called_once()
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
        stubs["_measure_redaction_rects"].side_effect = RuntimeError("boom")

        with self.assertLogs("scanning", level="WARNING"):
            services.run_compute_redactions(scan.pk)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.PAGE_COMPLETENESS_REVIEW_DONE)
        state = yolo.apply_state(yolo.live_detect_jobs(scan))
        self.assertEqual(state["attempts"], 1)
        self.assertIn("boom", state["last_error"])
        self.assertNotIn("applied_at", state)

    def test_a_counted_failure_promises_the_retry_it_gets(self):
        # PROCESSING, as the daemon's claim leaves it: the park writes
        # over the busy statuses only, so a scan created parked would
        # keep the message it had.
        scan, rows = merged_scan(status=Status.PROCESSING)
        stubs = self.patch_geometry()
        stubs["write_computed"].side_effect = RuntimeError("boom")

        with self.assertLogs("scanning", level="WARNING"):
            services.run_compute_redactions(scan.pk)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.PAGE_COMPLETENESS_REVIEW_DONE)
        self.assertIn("runs again by itself", scan.progress_message)

    def test_the_last_failure_says_it_stopped(self):
        """The trigger skips a run out of attempts, so a park that still
        promised a retry would have the curator waiting for nothing."""
        scan, rows = merged_scan(status=Status.PROCESSING)
        yolo.write_apply_state(rows, {"attempts": yolo.APPLY_MAX_ATTEMPTS - 1})
        stubs = self.patch_geometry()
        stubs["write_computed"].side_effect = RuntimeError("boom")

        with self.assertLogs("scanning", level="ERROR"):
            services.run_compute_redactions(scan.pk)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.PAGE_COMPLETENESS_REVIEW_DONE)
        self.assertIn("stopped", scan.progress_message)
        self.assertNotIn("by itself", scan.progress_message)
        with patch("scanning.s3_sync.s3_active", return_value=True):
            self.assertEqual(yolo.queue_ready_runs(), 0)

    def test_a_failed_apply_is_queued_again(self):
        scan, rows = merged_scan()
        stubs = self.patch_geometry()
        stubs["write_computed"].side_effect = RuntimeError("boom")
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
            apply,
            "load_detections_document",
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
