"""Tests for ``scanning.detections`` (issue #240, PR A).

The address of a detection, the curator's decisions about the model's
rows, and the resolution that lands them after an import. The compute
around them is tested in ``test_yolo_apply``.
"""

from django.test import TestCase

from scanning import detections
from scanning.factories import ScanFactory, UserFactory
from scanning.models import Detection, DetectionDecision, PageEdit
from scanning.tests.test_yolo_apply import glued_run, identity_map


def model_row(scan, **fields) -> Detection:
    """Store one model detection with an address.

    :param scan: The scan.
    :param fields: Overrides.
    :returns: The row.
    """
    values = {
        "scan": scan,
        "page_index": 0,
        "label": "KEY_ICON",
        "label_id": 1,
        "confidence": 0.8,
        "x0": 100.0,
        "y0": 100.0,
        "x1": 200.0,
        "y1": 200.0,
        "img_width": 1700,
        "img_height": 2200,
        "model_name": Detection.ModelName.BL_WARM,
        "found_by": [{"model": "bl_warm", "confidence": 0.8}],
        "source_page": 1,
        "source_fingerprint": scan.source_fingerprint,
    }
    values.update(fields)
    return Detection.objects.create(**values)


class TestIou(TestCase):
    def test_the_same_box_is_one(self):
        self.assertEqual(detections.iou([0, 0, 10, 10], [0, 0, 10, 10]), 1.0)

    def test_disjoint_boxes_are_zero(self):
        self.assertEqual(detections.iou([0, 0, 10, 10], [20, 20, 30, 30]), 0.0)

    def test_a_half_overlap(self):
        self.assertAlmostEqual(
            detections.iou([0, 0, 10, 10], [5, 0, 15, 10]), 1 / 3
        )

    def test_an_empty_box_is_zero(self):
        self.assertEqual(detections.iou([0, 0, 0, 0], [0, 0, 10, 10]), 0.0)


class TestSourceOfEntry(TestCase):
    """The address comes off the document, in either of its two shapes."""

    def test_a_glued_original_page(self):
        self.assertEqual(
            detections.source_of_entry(
                {"source": {"kind": "original", "pdf_page": 7}, "pdf_page": 5}
            ),
            (None, 7),
        )

    def test_a_glued_edit_page_is_one_based(self):
        self.assertEqual(
            detections.source_of_entry(
                {"source": {"kind": "edit", "edit_id": 42, "page": 0}}
            ),
            (42, 1),
        )

    def test_a_volume_document_names_the_original_by_pdf_page(self):
        self.assertEqual(
            detections.source_of_entry({"page_index": 4, "pdf_page": 5}),
            (None, 5),
        )

    def test_nothing_names_nothing(self):
        self.assertEqual(
            detections.source_of_entry({"page_index": 4}), (None, None)
        )


class TestSourceForIndex(TestCase):
    """A viewer's page index becomes an address through the run's map."""

    def test_the_original_space_is_the_identity(self):
        scan = ScanFactory(page_count=3)
        self.assertEqual(detections.source_for_index(scan, 2, None), (None, 3))

    def test_a_final_space_reads_the_map(self):
        scan = ScanFactory(page_count=3, source_fingerprint="10:3")
        edit = PageEdit.objects.create(
            scan=scan,
            kind=PageEdit.Kind.INSERT_PAGE,
            anchor_pdf_page=1,
            source_fingerprint="10:3",
        )
        page_map = {
            **identity_map(3),
            "final_page_count": 3,
            "deleted_pages": [2],
            "pages": [
                {
                    "final_page": 1,
                    "source": {"kind": "original", "pdf_page": 1},
                },
                {
                    "final_page": 2,
                    "source": {
                        "kind": "edit",
                        "edit_id": edit.pk,
                        "edit_kind": "insert_page",
                        "page": 0,
                    },
                },
                {
                    "final_page": 3,
                    "source": {"kind": "original", "pdf_page": 3},
                },
            ],
        }
        run = glued_run(scan, page_map=page_map)

        self.assertEqual(detections.source_for_index(scan, 0, run), (None, 1))
        self.assertEqual(
            detections.source_for_index(scan, 1, run), (edit.pk, 1)
        )
        self.assertEqual(detections.source_for_index(scan, 2, run), (None, 3))
        self.assertEqual(
            detections.source_for_index(scan, 3, run), (None, None)
        )

    def test_measured_run_is_none_without_a_ledger(self):
        scan = ScanFactory(page_count=2)
        glued_run(scan)
        self.assertIsNone(detections.measured_run(scan))


class TestDecide(TestCase):
    def setUp(self):
        self.scan = ScanFactory(page_count=2, source_fingerprint="10:2")
        self.user = UserFactory()

    def test_an_approval_writes_the_read_and_the_record(self):
        row = model_row(self.scan)

        decision = detections.decide(
            self.scan, row, DetectionDecision.Kind.APPROVE, self.user
        )

        row.refresh_from_db()
        self.assertEqual(row.confidence, 1.0)
        self.assertEqual(row.decision, decision)
        self.assertEqual(decision.target_bbox, [100, 100, 200, 200])
        self.assertEqual(decision.target_confidence, 0.8)
        self.assertEqual(decision.source_page, 1)
        self.assertEqual(decision.source_fingerprint, "10:2")
        self.assertEqual(decision.author, self.user)

    def test_the_same_decision_twice_is_one_row(self):
        row = model_row(self.scan)
        first = detections.decide(
            self.scan, row, DetectionDecision.Kind.APPROVE, self.user
        )
        row.refresh_from_db()

        second = detections.decide(
            self.scan, row, DetectionDecision.Kind.APPROVE, self.user
        )

        self.assertEqual(first, second)
        self.assertEqual(DetectionDecision.objects.count(), 1)

    def test_a_new_kind_withdraws_the_old_one(self):
        row = model_row(self.scan)
        first = detections.decide(
            self.scan, row, DetectionDecision.Kind.APPROVE, self.user
        )
        row.refresh_from_db()

        second = detections.decide(
            self.scan, row, DetectionDecision.Kind.DEACTIVATE, self.user
        )

        first.refresh_from_db()
        row.refresh_from_db()
        self.assertIsNotNone(first.withdrawn_at)
        self.assertEqual(first.withdrawn_by, self.user)
        self.assertEqual(row.decision, second)
        self.assertFalse(row.active)
        # The approval's confidence went back with it.
        self.assertEqual(row.confidence, 0.8)

    def test_a_row_the_map_does_not_hold_is_refused(self):
        """A decision with no address could never land, so it is not
        written (PR #288 review)."""
        run = glued_run(self.scan)
        row = model_row(self.scan, page_index=7, source_page=None)

        with self.assertLogs("scanning.detections", level="WARNING"):
            with self.assertRaises(detections.UnaddressableDetection):
                detections.decide(
                    self.scan,
                    row,
                    DetectionDecision.Kind.DEACTIVATE,
                    self.user,
                    run=run,
                )

        self.assertEqual(DetectionDecision.objects.count(), 0)
        row.refresh_from_db()
        self.assertTrue(row.active)

    def test_a_row_without_an_address_is_placed_by_position(self):
        row = model_row(self.scan, page_index=1, source_page=None)

        decision = detections.decide(
            self.scan, row, DetectionDecision.Kind.DEACTIVATE, self.user
        )

        self.assertEqual(decision.source_page, 2)
        self.assertIsNone(decision.source_edit)

    def test_withdraw_gives_the_row_back(self):
        row = model_row(self.scan)
        decision = detections.decide(
            self.scan, row, DetectionDecision.Kind.DEACTIVATE, self.user
        )

        count = detections.withdraw(
            DetectionDecision.objects.filter(pk=decision.pk), self.user
        )

        self.assertEqual(count, 1)
        row.refresh_from_db()
        self.assertTrue(row.active)
        self.assertIsNone(row.decision)
        self.assertEqual(
            detections.withdraw(
                DetectionDecision.objects.filter(pk=decision.pk), self.user
            ),
            0,
        )


class TestMoveAndManual(TestCase):
    def setUp(self):
        self.scan = ScanFactory(page_count=2, source_fingerprint="10:2")
        self.user = UserFactory()

    def test_a_move_is_a_deactivation_and_a_hand_drawn_row(self):
        row = model_row(self.scan)

        holder = detections.move_model_row(
            self.scan, row, [110.0, 110.0, 210.0, 210.0], self.user
        )

        row.refresh_from_db()
        self.assertFalse(row.active)
        self.assertEqual(holder.model_name, Detection.ModelName.MANUAL)
        self.assertEqual(holder.replaces, row.decision)
        self.assertEqual(holder.source_page, row.source_page)
        self.assertEqual(holder.label_id, row.label_id)
        self.assertEqual(holder.confidence, 1.0)
        self.assertEqual(holder.found_by, [])

    def test_withdrawing_the_hand_drawn_row_gives_the_model_box_back(self):
        row = model_row(self.scan)
        holder = detections.move_model_row(
            self.scan, row, [110.0, 110.0, 210.0, 210.0], self.user
        )

        self.assertTrue(detections.withdraw_manual(holder, self.user))

        holder.refresh_from_db()
        row.refresh_from_db()
        self.assertFalse(holder.active)
        self.assertEqual(holder.withdrawn_by, self.user)
        self.assertTrue(row.active)
        self.assertIsNone(row.decision)
        self.assertFalse(detections.withdraw_manual(holder, self.user))

    def test_add_manual_refuses_a_page_the_map_lacks(self):
        run = glued_run(self.scan)

        with self.assertLogs("scanning.detections", level="WARNING"):
            with self.assertRaises(detections.UnaddressableDetection):
                detections.add_manual(
                    self.scan,
                    7,
                    "KEY_ICON",
                    1,
                    [1.0, 2.0, 3.0, 4.0],
                    1,
                    1,
                    run=run,
                )

        self.assertEqual(Detection.objects.count(), 0)

    def test_add_manual_is_addressed_and_carries_no_family(self):
        row = detections.add_manual(
            self.scan, 1, "KEY_ICON", 1, [1.0, 2.0, 3.0, 4.0], 1700, 2200
        )

        self.assertEqual(row.source_page, 2)
        self.assertIsNone(row.source_edit)
        self.assertEqual(row.source_fingerprint, "10:2")
        self.assertEqual(row.found_by, [])
        self.assertTrue(row.active)


class TestResolve(TestCase):
    """The import deletes the model rows; the resolution lands the
    standing decisions on the new ones by address and overlap."""

    def setUp(self):
        self.scan = ScanFactory(page_count=2, source_fingerprint="10:2")

    def _decide(self, kind=DetectionDecision.Kind.DEACTIVATE, **fields):
        row = model_row(self.scan, **fields)
        decision = detections.decide(self.scan, row, kind, None)
        row.delete()
        return decision

    def test_no_decision_reads_no_row(self):
        """The common case: one query for the decisions, none for the
        tens of thousands of model rows (PR #288 review)."""
        model_row(self.scan)

        with self.assertNumQueries(2):
            landed, stale = detections.resolve(self.scan)

        self.assertEqual((landed, stale), (0, []))

    def test_reads_only_the_rows_a_decision_names(self):
        self._decide()
        model_row(self.scan)
        model_row(self.scan, page_index=1, source_page=2)
        model_row(self.scan, label="CASE_CAPTION", label_id=3)

        with self.assertNumQueries(4):
            landed, _ = detections.resolve(self.scan)

        self.assertEqual(landed, 1)

    def test_lands_on_the_same_box(self):
        decision = self._decide()
        new = model_row(self.scan)

        landed, stale = detections.resolve(self.scan)

        self.assertEqual((landed, stale), (1, []))
        new.refresh_from_db()
        self.assertEqual(new.decision, decision)
        self.assertFalse(new.active)

    def test_lands_on_a_box_the_snap_moved_a_little(self):
        self._decide()
        new = model_row(self.scan, x0=95.0, x1=205.0)

        landed, _ = detections.resolve(self.scan)

        self.assertEqual(landed, 1)
        new.refresh_from_db()
        self.assertFalse(new.active)

    def test_needs_the_same_page_and_label(self):
        self._decide()
        other_page = model_row(self.scan, page_index=1, source_page=2)
        other_label = model_row(self.scan, label="CASE_CAPTION", label_id=3)

        landed, stale = detections.resolve(self.scan)

        self.assertEqual(landed, 0)
        self.assertEqual(len(stale), 1)
        other_page.refresh_from_db()
        other_label.refresh_from_db()
        self.assertTrue(other_page.active)
        self.assertTrue(other_label.active)

    def test_a_box_below_the_threshold_is_stale(self):
        decision = self._decide()
        model_row(self.scan, x0=160.0, y0=160.0, x1=260.0, y1=260.0)

        with self.assertLogs("scanning.detections", level="WARNING"):
            landed, stale = detections.resolve(self.scan)

        self.assertEqual(landed, 0)
        self.assertEqual(stale, [decision])
        decision.refresh_from_db()
        self.assertIsNone(decision.withdrawn_at)

    def test_the_best_overlap_wins_and_each_box_is_taken_once(self):
        self._decide()
        self._decide(x0=300.0, x1=400.0)
        far = model_row(self.scan, x0=300.0, x1=400.0)
        near = model_row(self.scan)

        landed, stale = detections.resolve(self.scan)

        self.assertEqual((landed, stale), (2, []))
        far.refresh_from_db()
        near.refresh_from_db()
        self.assertFalse(far.active)
        self.assertFalse(near.active)

    def test_a_withdrawn_decision_lands_on_nothing(self):
        decision = self._decide()
        DetectionDecision.objects.filter(pk=decision.pk).update(
            withdrawn_at=decision.date_created
        )
        new = model_row(self.scan)

        landed, _ = detections.resolve(self.scan)

        self.assertEqual(landed, 0)
        new.refresh_from_db()
        self.assertTrue(new.active)

    def test_another_original_is_stale(self):
        decision = self._decide()
        DetectionDecision.objects.filter(pk=decision.pk).update(
            source_fingerprint="99:2"
        )
        new = model_row(self.scan)

        with self.assertLogs("scanning.detections", level="WARNING"):
            landed, stale = detections.resolve(self.scan)

        self.assertEqual((landed, stale), (0, [decision]))
        new.refresh_from_db()
        self.assertTrue(new.active)

    def test_a_hand_drawn_row_is_never_a_target(self):
        self._decide()
        manual = model_row(
            self.scan, model_name=Detection.ModelName.MANUAL, found_by=[]
        )

        landed, _ = detections.resolve(self.scan)

        self.assertEqual(landed, 0)
        manual.refresh_from_db()
        self.assertTrue(manual.active)

    def test_an_edit_page_is_its_own_address(self):
        edit = PageEdit.objects.create(
            scan=self.scan,
            kind=PageEdit.Kind.INSERT_PAGE,
            anchor_pdf_page=1,
            source_fingerprint="10:2",
        )
        self._decide(source_edit=edit, source_page=1)
        on_original = model_row(self.scan)
        on_edit = model_row(self.scan, source_edit=edit, source_page=1)

        landed, _ = detections.resolve(self.scan)

        self.assertEqual(landed, 1)
        on_original.refresh_from_db()
        on_edit.refresh_from_db()
        self.assertTrue(on_original.active)
        self.assertFalse(on_edit.active)


class TestRelocateManualRows(TestCase):
    """After an import under a new run, a hand-drawn row moves to its
    page in the new space by its address (PR #288 review)."""

    def setUp(self):
        self.scan = ScanFactory(page_count=3, source_fingerprint="10:3")

    def _manual(self, **fields):
        return model_row(
            self.scan,
            model_name=Detection.ModelName.MANUAL,
            found_by=[],
            confidence=1.0,
            **fields,
        )

    def _run_without_page_2(self):
        page_map = {
            **identity_map(3),
            "final_page_count": 2,
            "deleted_pages": [2],
            "pages": [
                {
                    "final_page": 1,
                    "source": {"kind": "original", "pdf_page": 1},
                },
                {
                    "final_page": 2,
                    "source": {"kind": "original", "pdf_page": 3},
                },
            ],
        }
        return glued_run(self.scan, number=2, page_map=page_map)

    def test_a_box_after_a_deletion_moves_up_one_page(self):
        row = self._manual(page_index=2, source_page=3)
        run = self._run_without_page_2()

        moved, unplaced = detections.relocate_manual_rows(self.scan, run)

        self.assertEqual((moved, unplaced), (1, []))
        row.refresh_from_db()
        self.assertEqual(row.page_index, 1)
        self.assertEqual(row.apply_run, run)
        self.assertEqual(row.source_page, 3)

    def test_a_box_on_the_deleted_page_is_left_and_logged(self):
        row = self._manual(page_index=1, source_page=2)
        run = self._run_without_page_2()

        with self.assertLogs("scanning.detections", level="WARNING") as logs:
            moved, unplaced = detections.relocate_manual_rows(self.scan, run)

        self.assertEqual(moved, 0)
        self.assertEqual(unplaced, [row])
        self.assertIn(f"#{row.pk}", logs.output[0])
        row.refresh_from_db()
        self.assertEqual(row.page_index, 1)
        self.assertTrue(row.active)

    def test_a_box_with_no_address_is_left(self):
        self._manual(page_index=2, source_page=None)
        run = self._run_without_page_2()

        with self.assertLogs("scanning.detections", level="WARNING"):
            moved, unplaced = detections.relocate_manual_rows(self.scan, run)

        self.assertEqual((moved, len(unplaced)), (0, 1))

    def test_a_box_of_another_original_is_left(self):
        self._manual(page_index=2, source_page=3, source_fingerprint="99:3")
        run = self._run_without_page_2()

        with self.assertLogs("scanning.detections", level="WARNING"):
            moved, unplaced = detections.relocate_manual_rows(self.scan, run)

        self.assertEqual((moved, len(unplaced)), (0, 1))

    def test_a_withdrawn_box_is_not_touched(self):
        row = self._manual(page_index=2, source_page=3, active=False)
        Detection.objects.filter(pk=row.pk).update(
            withdrawn_at=row.date_created
        )
        run = self._run_without_page_2()

        moved, unplaced = detections.relocate_manual_rows(self.scan, run)

        self.assertEqual((moved, unplaced), (0, []))
        row.refresh_from_db()
        self.assertEqual(row.page_index, 2)

    def test_a_box_on_an_edit_page_follows_the_slot(self):
        edit = PageEdit.objects.create(
            scan=self.scan,
            kind=PageEdit.Kind.INSERT_PAGE,
            anchor_pdf_page=1,
            source_fingerprint="10:3",
        )
        row = self._manual(page_index=1, source_edit=edit, source_page=1)
        page_map = {
            **identity_map(3),
            "final_page_count": 4,
            "deleted_pages": [],
            "pages": [
                {
                    "final_page": 1,
                    "source": {"kind": "original", "pdf_page": 1},
                },
                {
                    "final_page": 2,
                    "source": {"kind": "original", "pdf_page": 2},
                },
                {
                    "final_page": 3,
                    "source": {
                        "kind": "edit",
                        "edit_id": edit.pk,
                        "edit_kind": "insert_page",
                        "page": 0,
                    },
                },
                {
                    "final_page": 4,
                    "source": {"kind": "original", "pdf_page": 3},
                },
            ],
        }
        run = glued_run(self.scan, number=2, page_map=page_map)

        moved, unplaced = detections.relocate_manual_rows(self.scan, run)

        self.assertEqual((moved, unplaced), (1, []))
        row.refresh_from_db()
        self.assertEqual(row.page_index, 2)

    def test_a_box_on_a_replaced_page_does_not_carry(self):
        """The content is new, so ``originals_to_final`` leaves the page
        out, and the box is logged."""
        edit = PageEdit.objects.create(
            scan=self.scan,
            kind=PageEdit.Kind.REPLACE_PAGE,
            pdf_page=2,
            source_fingerprint="10:3",
        )
        self._manual(page_index=1, source_page=2)
        page_map = {
            **identity_map(3),
            "pages": [
                {
                    "final_page": 1,
                    "source": {"kind": "original", "pdf_page": 1},
                },
                {
                    "final_page": 2,
                    "source": {
                        "kind": "edit",
                        "edit_id": edit.pk,
                        "edit_kind": "replace_page",
                        "page": 0,
                        "pdf_page": 2,
                    },
                },
                {
                    "final_page": 3,
                    "source": {"kind": "original", "pdf_page": 3},
                },
            ],
        }
        run = glued_run(self.scan, number=2, page_map=page_map)

        with self.assertLogs("scanning.detections", level="WARNING"):
            moved, unplaced = detections.relocate_manual_rows(self.scan, run)

        self.assertEqual((moved, len(unplaced)), (0, 1))
