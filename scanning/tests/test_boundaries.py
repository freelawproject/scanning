"""Tests for ``scanning.boundaries`` (issue #240, PR C).

The opinion boundaries as rows: the compute's write, the resolution
of the curator's dismissals, the moves, the readers, and the three
endpoints. The compute around them is tested in ``test_yolo_apply``.
"""

from blackletter.models import BBox, Label, Page
from blackletter.models import Detection as BLDetection
from blackletter.models import Document as BLDoc
from django.test import TestCase

from scanning import boundaries
from scanning.factories import (
    OpinionBoundaryFactory,
    ScanFactory,
    UserFactory,
)
from scanning.models import Detection, OpinionBoundary, PageEdit
from scanning.tests.test_detections import model_row
from scanning.tests.test_yolo_apply import glued_run, identity_map

IMG_W, IMG_H = 1700, 2200
PAGE_W, PAGE_H = 612.0, 792.0


def caption_row(scan, page_index, x0=100.0, y0=200.0, **fields):
    """Store one caption detection on ``page_index``.

    :param scan: The scan.
    :param page_index: The 0-based page.
    :param x0: The box's left, in pixels.
    :param y0: The box's top, in pixels.
    :param fields: Overrides.
    :returns: The row.
    """
    values = {
        "page_index": page_index,
        "source_page": page_index + 1,
        "label": "CASE_CAPTION",
        "label_id": int(Label.CASE_CAPTION),
        "confidence": 0.95,
        "x0": x0,
        "y0": y0,
        "x1": x0 + 600,
        "y1": y0 + 150,
        "img_width": IMG_W,
        "img_height": IMG_H,
    }
    values.update(fields)
    return model_row(scan, **values)


def key_row(scan, page_index, x0=900.0, y0=1800.0, **fields):
    """Store one key icon detection on ``page_index``.

    :param scan: The scan.
    :param page_index: The 0-based page.
    :param x0: The box's left, in pixels.
    :param y0: The box's top, in pixels.
    :param fields: Overrides.
    :returns: The row.
    """
    values = {
        "page_index": page_index,
        "source_page": page_index + 1,
        "label": "KEY_ICON",
        "label_id": int(Label.KEY_ICON),
        "confidence": 0.95,
        "x0": x0,
        "y0": y0,
        # A key icon is wider than tall, or blackletter's shape filter
        # drops it.
        "x1": x0 + 80,
        "y1": y0 + 40,
        "img_width": IMG_W,
        "img_height": IMG_H,
    }
    values.update(fields)
    return model_row(scan, **values)


def document_of(rows) -> tuple[BLDoc, dict[int, int]]:
    """Build the snapped document the compute would build from ``rows``.

    :param rows: ``Detection`` rows.
    :returns: The document and the ``{id(bl_detection): pk}`` map.
    """
    pages: dict[int, Page] = {}
    row_ids: dict[int, int] = {}
    for row in rows:
        page = pages.get(row.page_index)
        if page is None:
            page = Page(
                index=row.page_index,
                pdf_width=PAGE_W,
                pdf_height=PAGE_H,
                img_width=IMG_W,
                img_height=IMG_H,
            )
            pages[row.page_index] = page
        det = BLDetection(
            bbox=BBox(x1=row.x0, y1=row.y0, x2=row.x1, y2=row.y1),
            label=Label(row.label_id),
            confidence=row.confidence,
            page_index=row.page_index,
        )
        page.detections.append(det)
        row_ids[id(det)] = row.pk
    document = BLDoc(
        pdf_path="/tmp/x.pdf",
        pages=[pages[i] for i in sorted(pages)],
        bl_warm=True,
    )
    return document, row_ids


def _withdrawn_dismissal(scan, user):
    """Return a human row that is not an addition: a dismissal.

    :param scan: The scan.
    :param user: The curator.
    :returns: The dismissal of a fresh computed row.
    """
    row = OpinionBoundaryFactory(scan=scan)
    return boundaries.dismiss(scan, row, user)


def one_opinion(scan, start=0, end=1):
    """Store a caption on ``start`` and a key on ``end`` and pair them.

    :param scan: The scan.
    :param start: The caption's page.
    :param end: The key's page.
    :returns: ``(caption, key, boundary)``.
    """
    caption = caption_row(scan, start)
    key = key_row(scan, end)
    document, row_ids = document_of([caption, key])
    boundaries.write_computed(scan, document, row_ids, None, 1)
    return caption, key, OpinionBoundary.objects.computed().get(scan=scan)


class TestToPoints(TestCase):
    def test_the_page_size_gives_the_exact_scale(self):
        x, y = boundaries.to_points(850, 1100, IMG_W, IMG_H, PAGE_W, PAGE_H)
        self.assertAlmostEqual(x, 306.0)
        self.assertAlmostEqual(y, 396.0)

    def test_without_the_page_size_the_render_dpi_answers(self):
        x, y = boundaries.to_points(200, 400, IMG_W, IMG_H)
        self.assertAlmostEqual(x, 72.0)
        self.assertAlmostEqual(y, 144.0)

    def test_the_two_answers_differ_by_less_than_a_point(self):
        exact = boundaries.to_points(
            IMG_W, IMG_H, IMG_W, IMG_H, PAGE_W, PAGE_H
        )
        guessed = boundaries.to_points(IMG_W, IMG_H, IMG_W, IMG_H)
        self.assertLess(abs(exact[0] - guessed[0]), 1.0)
        self.assertLess(abs(exact[1] - guessed[1]), 1.0)


class TestPlacer(TestCase):
    def test_the_original_space_is_the_identity(self):
        place = boundaries.placer(None)
        self.assertEqual(place(None, 3), 2)
        self.assertIsNone(place(None, None))
        self.assertIsNone(place(7, 1))

    def test_a_deletion_shifts_the_pages_after_it(self):
        scan = ScanFactory(page_count=3)
        page_map = identity_map(3)
        del page_map["pages"][1]
        page_map["pages"][1]["final_page"] = 2
        page_map["final_page_count"] = 2
        run = glued_run(scan, page_map=page_map)

        place = boundaries.placer(run)

        self.assertEqual(place(None, 1), 0)
        self.assertIsNone(place(None, 2))
        self.assertEqual(place(None, 3), 1)

    def test_an_edit_page_is_placed_by_its_slot(self):
        scan = ScanFactory(page_count=2)
        edit = PageEdit.objects.create(
            scan=scan,
            kind=PageEdit.Kind.INSERT_PAGE,
            anchor_pdf_page=1,
            author=UserFactory(),
        )
        page_map = identity_map(2)
        page_map["pages"].insert(
            1,
            {
                "final_page": 2,
                "source": {"kind": "edit", "edit_id": edit.pk, "page": 0},
            },
        )
        page_map["pages"][2]["final_page"] = 3
        page_map["final_page_count"] = 3
        run = glued_run(scan, page_map=page_map)

        place = boundaries.placer(run)

        self.assertEqual(place(edit.pk, 1), 1)
        self.assertEqual(place(None, 2), 2)


class TestWriteComputed(TestCase):
    """The compute writes one row per pair, with exact FKs and points."""

    def test_one_row_per_pair_with_the_anchors_in_points(self):
        scan = ScanFactory(page_count=2)
        caption, key, row = one_opinion(scan)

        self.assertEqual(row.start_page_index, 0)
        self.assertEqual(row.end_page_index, 1)
        self.assertEqual(row.start_address, (None, 1))
        self.assertEqual(row.end_address, (None, 2))
        self.assertAlmostEqual(row.start_x, caption.x0 * PAGE_W / IMG_W)
        self.assertAlmostEqual(row.start_y, caption.y0 * PAGE_H / IMG_H)
        self.assertAlmostEqual(row.end_x, key.x1 * PAGE_W / IMG_W)
        self.assertAlmostEqual(row.end_y, key.y1 * PAGE_H / IMG_H)
        self.assertEqual(row.start_detection_id, caption.pk)
        self.assertEqual(row.end_detection_id, key.pk)
        self.assertEqual(row.ordinal, 0)
        self.assertEqual(row.detect_run, 1)
        self.assertIsNone(row.apply_run)
        self.assertEqual(row.origin, OpinionBoundary.Origin.COMPUTED)
        self.assertEqual(row.kind, "")

    def test_a_second_compute_rewrites_the_computed_rows_and_keeps_the_human_ones(
        self,
    ):
        scan = ScanFactory(page_count=2)
        caption, key, first = one_opinion(scan)
        added = OpinionBoundaryFactory(
            scan=scan,
            origin=OpinionBoundary.Origin.HUMAN,
            kind=OpinionBoundary.Kind.ADD,
            ordinal=None,
        )

        document, row_ids = document_of([caption, key])
        boundaries.write_computed(scan, document, row_ids, None, 2)

        computed = OpinionBoundary.objects.computed().filter(scan=scan)
        self.assertEqual(computed.count(), 1)
        self.assertNotEqual(computed.get().pk, first.pk)
        self.assertEqual(computed.get().detect_run, 2)
        self.assertTrue(OpinionBoundary.objects.filter(pk=added.pk).exists())

    def test_the_pairs_are_returned_for_the_geometry(self):
        scan = ScanFactory(page_count=2)
        caption = caption_row(scan, 0)
        key = key_row(scan, 1)
        document, row_ids = document_of([caption, key])

        pairs = boundaries.write_computed(scan, document, row_ids, None, 1)

        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0][0].label, Label.CASE_CAPTION)
        self.assertEqual(pairs[0][1].label, Label.KEY_ICON)

    def test_an_empty_document_writes_nothing_and_clears_the_old_rows(self):
        scan = ScanFactory(page_count=2)
        OpinionBoundaryFactory(scan=scan)

        pairs = boundaries.write_computed(
            scan, BLDoc(pdf_path="/tmp/x.pdf", pages=[]), {}, None, 1
        )

        self.assertEqual(pairs, [])
        self.assertFalse(
            OpinionBoundary.objects.computed().filter(scan=scan).exists()
        )

    def test_the_rows_are_written_in_the_run_space(self):
        scan = ScanFactory(page_count=3)
        page_map = identity_map(3)
        del page_map["pages"][0]
        for i, entry in enumerate(page_map["pages"], start=1):
            entry["final_page"] = i
        page_map["final_page_count"] = 2
        run = glued_run(scan, page_map=page_map)
        # In the final space: page 0 is original page 2.
        caption = caption_row(scan, 0, source_page=2)
        key = key_row(scan, 1, source_page=3)
        document, row_ids = document_of([caption, key])

        boundaries.write_computed(scan, document, row_ids, run, 1)

        row = OpinionBoundary.objects.computed().get(scan=scan)
        self.assertEqual(row.apply_run, run)
        self.assertEqual(row.start_address, (None, 2))
        self.assertEqual(row.end_address, (None, 3))
        self.assertEqual((row.start_page_index, row.end_page_index), (0, 1))


class TestDismissAndResolve(TestCase):
    """A dismissal hides the row now, and lands on the rebuilt one later."""

    def setUp(self):
        self.user = UserFactory()
        self.scan = ScanFactory(page_count=2)
        self.caption, self.key, self.row = one_opinion(self.scan)

    def test_a_dismissal_copies_the_anchors_and_hides_the_row_at_once(self):
        dismissal = boundaries.dismiss(self.scan, self.row, self.user)

        self.row.refresh_from_db()
        self.assertEqual(self.row.decision, dismissal)
        self.assertTrue(self.row.is_dismissed)
        self.assertEqual(dismissal.origin, OpinionBoundary.Origin.HUMAN)
        self.assertEqual(dismissal.kind, OpinionBoundary.Kind.DISMISS)
        self.assertEqual(dismissal.start_address, self.row.start_address)
        self.assertEqual(dismissal.start_x, self.row.start_x)
        self.assertEqual(dismissal.end_y, self.row.end_y)
        self.assertEqual(dismissal.author, self.user)

    def test_a_second_dismissal_is_the_standing_one(self):
        first = boundaries.dismiss(self.scan, self.row, self.user)
        second = boundaries.dismiss(self.scan, self.row, self.user)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(
            OpinionBoundary.objects.human().filter(scan=self.scan).count(), 1
        )

    def test_a_restore_withdraws_the_dismissal_and_frees_the_row(self):
        dismissal = boundaries.dismiss(self.scan, self.row, self.user)

        self.assertTrue(boundaries.restore(self.scan, self.row, self.user))

        self.row.refresh_from_db()
        dismissal.refresh_from_db()
        self.assertIsNone(self.row.decision)
        self.assertIsNotNone(dismissal.withdrawn_at)
        self.assertEqual(dismissal.withdrawn_by, self.user)
        # Nothing is deleted.
        self.assertTrue(
            OpinionBoundary.objects.filter(pk=dismissal.pk).exists()
        )
        self.assertFalse(boundaries.restore(self.scan, self.row, self.user))

    def test_the_dismissal_lands_on_the_rebuilt_row(self):
        dismissal = boundaries.dismiss(self.scan, self.row, self.user)

        document, row_ids = document_of([self.caption, self.key])
        boundaries.write_computed(self.scan, document, row_ids, None, 2)

        rebuilt = OpinionBoundary.objects.computed().get(scan=self.scan)
        self.assertNotEqual(rebuilt.pk, self.row.pk)
        self.assertEqual(rebuilt.decision, dismissal)

    def test_a_moved_caption_within_the_tolerance_still_lands(self):
        dismissal = boundaries.dismiss(self.scan, self.row, self.user)
        # Ten pixels at 612/1700 points per pixel: under four points.
        Detection.objects.filter(pk=self.caption.pk).update(
            x0=self.caption.x0 + 10
        )
        self.caption.refresh_from_db()

        document, row_ids = document_of([self.caption, self.key])
        boundaries.write_computed(self.scan, document, row_ids, None, 2)

        rebuilt = OpinionBoundary.objects.computed().get(scan=self.scan)
        self.assertEqual(rebuilt.decision, dismissal)

    def test_a_caption_beyond_the_tolerance_leaves_the_dismissal_stale(self):
        boundaries.dismiss(self.scan, self.row, self.user)
        Detection.objects.filter(pk=self.caption.pk).update(
            y0=self.caption.y0 + 400
        )
        self.caption.refresh_from_db()
        document, row_ids = document_of([self.caption, self.key])

        with self.assertLogs("scanning.boundaries", level="WARNING") as logs:
            boundaries.write_computed(self.scan, document, row_ids, None, 2)

        rebuilt = OpinionBoundary.objects.computed().get(scan=self.scan)
        self.assertIsNone(rebuilt.decision)
        self.assertIn("found no boundary to land on", logs.output[0])

    def test_a_dismissal_of_another_original_lands_nowhere(self):
        dismissal = boundaries.dismiss(self.scan, self.row, self.user)
        OpinionBoundary.objects.filter(pk=dismissal.pk).update(
            source_fingerprint="old"
        )
        self.scan.source_fingerprint = "new"
        self.scan.save(update_fields=["source_fingerprint"])

        document, row_ids = document_of([self.caption, self.key])
        with self.assertLogs("scanning.boundaries", level="WARNING"):
            boundaries.write_computed(self.scan, document, row_ids, None, 2)

        rebuilt = OpinionBoundary.objects.computed().get(scan=self.scan)
        self.assertIsNone(rebuilt.decision)

    def test_each_rebuilt_row_is_taken_once(self):
        """Two dismissals at one address land on two rows, not one."""
        # A second opinion that starts on the same page: the first one
        # closes on page 0 (a key below its caption, in the same left
        # column, since reading order walks a column top to bottom), the
        # second opens below that key and closes on page 1.
        first_key = key_row(self.scan, 0, x0=200.0, y0=900.0)
        second_caption = caption_row(self.scan, 0, y0=1300.0)
        document, row_ids = document_of(
            [self.caption, first_key, second_caption, self.key]
        )
        boundaries.write_computed(self.scan, document, row_ids, None, 1)
        rows = list(OpinionBoundary.objects.computed().filter(scan=self.scan))
        self.assertEqual(len(rows), 2)
        for row in rows:
            boundaries.dismiss(self.scan, row, self.user)

        boundaries.write_computed(self.scan, document, row_ids, None, 2)

        decisions = {
            r.decision_id
            for r in OpinionBoundary.objects.computed().filter(scan=self.scan)
        }
        self.assertEqual(len(decisions), 2)
        self.assertNotIn(None, decisions)

    def test_a_dismissal_of_an_unaddressed_boundary_is_refused(self):
        """It could never land: ``resolve`` matches by the start address
        (the ``detections.decide`` rule)."""
        OpinionBoundary.objects.filter(pk=self.row.pk).update(
            start_source_page=None
        )
        self.row.refresh_from_db()

        with self.assertRaises(boundaries.UnaddressableBoundary):
            boundaries.dismiss(self.scan, self.row, self.user)

        self.assertFalse(
            OpinionBoundary.objects.human().filter(scan=self.scan).exists()
        )

    def test_resolve_reads_no_row_when_no_dismissal_stands(self):
        with self.assertNumQueries(1):
            self.assertEqual(boundaries.resolve(self.scan), (0, []))


class TestAdd(TestCase):
    """The curator draws a boundary, or moves one anchor."""

    def setUp(self):
        self.user = UserFactory()
        self.scan = ScanFactory(page_count=3)
        self.caption, self.key, self.row = one_opinion(self.scan)

    def test_an_addition_from_two_detections(self):
        other_key = key_row(self.scan, 2)

        row = boundaries.add(
            self.scan, self.caption, other_key, self.user, None
        )

        self.assertEqual(row.origin, OpinionBoundary.Origin.HUMAN)
        self.assertEqual(row.kind, OpinionBoundary.Kind.ADD)
        self.assertEqual(row.start_detection, self.caption)
        self.assertEqual(row.end_detection, other_key)
        self.assertEqual((row.start_page_index, row.end_page_index), (0, 2))
        self.assertEqual(row.end_address, (None, 3))
        self.assertIsNone(row.replaces)
        # The DPI rule, since no page size is known here.
        self.assertAlmostEqual(row.start_x, self.caption.x0 * 72 / 200)

    def test_an_addition_from_two_points(self):
        row = boundaries.add(
            self.scan, (0, 50.0, 60.0), (2, 500.0, 700.0), self.user, None
        )

        self.assertEqual((row.start_x, row.start_y), (50.0, 60.0))
        self.assertEqual((row.end_x, row.end_y), (500.0, 700.0))
        self.assertIsNone(row.start_detection)
        self.assertEqual(row.start_address, (None, 1))

    def test_a_move_dismisses_the_computed_row_and_names_the_dismissal(self):
        other_key = key_row(self.scan, 2)

        row = boundaries.add(
            self.scan,
            self.caption,
            other_key,
            self.user,
            None,
            replaces=self.row,
        )

        self.row.refresh_from_db()
        self.assertIsNotNone(self.row.decision)
        self.assertEqual(row.replaces, self.row.decision)

    def test_withdrawing_the_addition_gives_the_computed_row_back(self):
        other_key = key_row(self.scan, 2)
        row = boundaries.add(
            self.scan,
            self.caption,
            other_key,
            self.user,
            None,
            replaces=self.row,
        )

        self.assertIsNone(boundaries.dismiss(self.scan, row, self.user))

        row.refresh_from_db()
        self.row.refresh_from_db()
        self.assertIsNotNone(row.withdrawn_at)
        self.assertIsNone(self.row.decision)
        # A second withdrawal is a no-op.
        self.assertIsNone(boundaries.dismiss(self.scan, row, self.user))

    def test_a_second_move_leaves_one_boundary(self):
        """Replacing a curator's addition withdraws it alone and carries
        its dismissal, so the computed boundary does not come back
        beside the new addition."""
        key_two = key_row(self.scan, 2)
        key_one = key_row(self.scan, 1, y0=600.0)
        first = boundaries.add(
            self.scan,
            self.caption,
            key_two,
            self.user,
            None,
            replaces=self.row,
        )

        second = boundaries.add(
            self.scan, self.caption, key_one, self.user, None, replaces=first
        )

        first.refresh_from_db()
        self.row.refresh_from_db()
        self.assertIsNotNone(first.withdrawn_at)
        self.assertEqual(second.replaces, first.replaces)
        self.assertTrue(self.row.is_dismissed)
        live = [(r.origin, r.kind) for r in boundaries.standing(self.scan)]
        self.assertEqual(live, [("human", "add")])

        # Dismissing the last addition gives the computed boundary back.
        boundaries.dismiss(self.scan, second, self.user)
        self.row.refresh_from_db()
        self.assertFalse(self.row.is_dismissed)
        self.assertEqual(
            [r.pk for r in boundaries.standing(self.scan)], [self.row.pk]
        )

    def test_a_move_of_a_plain_addition_carries_no_dismissal(self):
        plain = boundaries.add(
            self.scan, (0, 1.0, 1.0), (2, 1.0, 1.0), self.user, None
        )

        moved = boundaries.add(
            self.scan,
            (0, 1.0, 1.0),
            (1, 1.0, 1.0),
            self.user,
            None,
            replaces=plain,
        )

        plain.refresh_from_db()
        self.assertIsNotNone(plain.withdrawn_at)
        self.assertIsNone(moved.replaces)

    def test_a_dismissal_cannot_be_replaced(self):
        """Only a boundary or an addition stands in for a moved one."""
        dismissal = _withdrawn_dismissal(self.scan, self.user)
        with self.assertRaises(ValueError):
            boundaries.add(
                self.scan,
                (0, 1.0, 1.0),
                (1, 1.0, 1.0),
                self.user,
                None,
                replaces=dismissal,
            )

    def test_an_end_above_the_start_on_one_page_is_refused(self):
        with self.assertRaises(boundaries.MisorderedBoundary):
            boundaries.add(
                self.scan, (1, 50.0, 500.0), (1, 50.0, 100.0), self.user, None
            )

    def test_an_end_high_in_the_right_column_follows_a_low_start_at_left(
        self,
    ):
        """Reading order walks the left column first, so a key high in
        the right column closes a caption low in the left one."""
        for x0, x1 in ((100, 800), (900, 1600)):
            model_row(
                self.scan,
                label="TEXT_COLUMN",
                label_id=int(Label.TEXT_COLUMN),
                page_index=1,
                source_page=2,
                x0=x0,
                y0=100,
                x1=x1,
                y1=2100,
                img_width=IMG_W,
                img_height=IMG_H,
            )

        row = boundaries.add(
            self.scan, (1, 50.0, 500.0), (1, 400.0, 100.0), self.user, None
        )

        self.assertEqual(row.start_page_index, row.end_page_index)

    def test_a_page_outside_the_map_is_refused(self):
        with self.assertRaises(boundaries.UnaddressableBoundary):
            boundaries.add(
                self.scan,
                (0, 1.0, 1.0),
                (9, 1.0, 1.0),
                self.user,
                glued_run(self.scan),
            )

    def test_an_end_before_the_start_is_refused(self):
        with self.assertRaises(boundaries.MisorderedBoundary):
            boundaries.add(
                self.scan, (2, 1.0, 1.0), (0, 1.0, 1.0), self.user, None
            )


class TestRelocateHumanRows(TestCase):
    def test_an_addition_follows_a_deletion_into_the_new_space(self):
        scan = ScanFactory(page_count=3)
        added = OpinionBoundaryFactory(
            scan=scan,
            origin=OpinionBoundary.Origin.HUMAN,
            kind=OpinionBoundary.Kind.ADD,
            ordinal=None,
            start_source_page=2,
            start_page_index=1,
            end_source_page=3,
            end_page_index=2,
        )
        page_map = identity_map(3)
        del page_map["pages"][0]
        for i, entry in enumerate(page_map["pages"], start=1):
            entry["final_page"] = i
        page_map["final_page_count"] = 2
        run = glued_run(scan, page_map=page_map)

        moved, unplaced = boundaries.relocate_human_rows(scan, run)

        self.assertEqual((moved, unplaced), (1, []))
        added.refresh_from_db()
        self.assertEqual(
            (added.start_page_index, added.end_page_index), (0, 1)
        )
        self.assertEqual(added.apply_run, run)

    def test_a_row_on_a_deleted_page_is_left_and_logged(self):
        scan = ScanFactory(page_count=3)
        added = OpinionBoundaryFactory(
            scan=scan,
            origin=OpinionBoundary.Origin.HUMAN,
            kind=OpinionBoundary.Kind.ADD,
            ordinal=None,
            start_source_page=1,
            start_page_index=0,
            end_source_page=2,
            end_page_index=1,
        )
        page_map = identity_map(3)
        del page_map["pages"][0]
        for i, entry in enumerate(page_map["pages"], start=1):
            entry["final_page"] = i
        run = glued_run(scan, page_map=page_map)

        with self.assertLogs("scanning.boundaries", level="WARNING"):
            moved, unplaced = boundaries.relocate_human_rows(scan, run)

        self.assertEqual(moved, 0)
        self.assertEqual([r.pk for r in unplaced], [added.pk])
        added.refresh_from_db()
        self.assertEqual(added.start_page_index, 0)
        self.assertIsNone(added.apply_run)

    def test_a_stale_row_is_left(self):
        scan = ScanFactory(page_count=2, source_fingerprint="new")
        OpinionBoundaryFactory(
            scan=scan,
            origin=OpinionBoundary.Origin.HUMAN,
            kind=OpinionBoundary.Kind.ADD,
            ordinal=None,
            source_fingerprint="old",
        )
        with self.assertLogs("scanning.boundaries", level="WARNING"):
            moved, unplaced = boundaries.relocate_human_rows(
                scan, glued_run(scan)
            )
        self.assertEqual((moved, len(unplaced)), (0, 1))

    def test_an_identity_run_writes_nothing(self):
        scan = ScanFactory(page_count=2)
        run = glued_run(scan)
        added = OpinionBoundaryFactory(
            scan=scan,
            origin=OpinionBoundary.Origin.HUMAN,
            kind=OpinionBoundary.Kind.ADD,
            ordinal=None,
            apply_run=run,
        )
        before = added.date_modified

        self.assertEqual(boundaries.relocate_human_rows(scan, run), (0, []))

        added.refresh_from_db()
        self.assertEqual(added.date_modified, before)


class TestStanding(TestCase):
    """The reader every consumer goes through."""

    def test_reading_order_across_two_columns(self):
        scan = ScanFactory(page_count=1)
        # Two column boxes: the divide is at 850 px = 306 pt.
        model_row(
            scan,
            label="TEXT_COLUMN",
            label_id=int(Label.TEXT_COLUMN),
            x0=100,
            y0=100,
            x1=800,
            y1=2100,
            img_width=IMG_W,
            img_height=IMG_H,
        )
        model_row(
            scan,
            label="TEXT_COLUMN",
            label_id=int(Label.TEXT_COLUMN),
            x0=900,
            y0=100,
            x1=1600,
            y1=2100,
            img_width=IMG_W,
            img_height=IMG_H,
        )
        right_high = OpinionBoundaryFactory(
            scan=scan, start_x=400.0, start_y=100.0, end_page_index=0
        )
        left_low = OpinionBoundaryFactory(
            scan=scan, start_x=50.0, start_y=600.0, end_page_index=0
        )

        rows = boundaries.standing(scan)

        self.assertEqual([r.pk for r in rows], [left_low.pk, right_high.pk])

    def test_one_column_orders_by_y(self):
        scan = ScanFactory(page_count=1)
        right_high = OpinionBoundaryFactory(
            scan=scan, start_x=400.0, start_y=100.0, end_page_index=0
        )
        left_low = OpinionBoundaryFactory(
            scan=scan, start_x=50.0, start_y=600.0, end_page_index=0
        )

        rows = boundaries.standing(scan)

        self.assertEqual([r.pk for r in rows], [right_high.pk, left_low.pk])

    def test_a_computed_row_a_move_replaced_is_left_out(self):
        user = UserFactory()
        scan = ScanFactory(page_count=3)
        caption, key, row = one_opinion(scan)
        other_key = key_row(scan, 2)
        moved = boundaries.add(
            scan, caption, other_key, user, None, replaces=row
        )

        self.assertEqual([r.pk for r in boundaries.standing(scan)], [moved.pk])

        # A plain dismissal, with no replacement, keeps its muted card.
        boundaries.dismiss(scan, moved, user)
        boundaries.dismiss(scan, row, user)
        rows = boundaries.standing(scan)
        self.assertEqual([r.pk for r in rows], [row.pk])
        self.assertTrue(rows[0].is_dismissed)

    def test_dismissed_rows_are_kept_and_flagged_withdrawn_additions_are_not(
        self,
    ):
        user = UserFactory()
        scan = ScanFactory(page_count=2)
        computed = OpinionBoundaryFactory(scan=scan)
        boundaries.dismiss(scan, computed, user)
        added = OpinionBoundaryFactory(
            scan=scan,
            origin=OpinionBoundary.Origin.HUMAN,
            kind=OpinionBoundary.Kind.ADD,
            ordinal=None,
            start_y=300.0,
        )
        withdrawn = OpinionBoundaryFactory(
            scan=scan,
            origin=OpinionBoundary.Origin.HUMAN,
            kind=OpinionBoundary.Kind.ADD,
            ordinal=None,
            start_y=500.0,
        )
        boundaries.dismiss(scan, withdrawn, user)

        rows = boundaries.standing(scan)

        self.assertEqual([r.pk for r in rows], [computed.pk, added.pk])
        self.assertTrue(rows[0].is_dismissed)
        self.assertFalse(rows[1].is_dismissed)


class TestViewerPayload(TestCase):
    def test_the_legacy_keys_and_the_ids(self):
        scan = ScanFactory(page_count=2, start_page=1)
        caption, key, row = one_opinion(scan)
        model_row(scan, label="IMAGE", label_id=int(Label.IMAGE), page_index=1)

        payload = boundaries.viewer_payload(
            scan, {0: (101, None), 1: (102, 103)}
        )

        self.assertEqual(len(payload), 1)
        op = payload[0]
        self.assertEqual(op["id"], row.pk)
        self.assertEqual(op["origin"], "computed")
        self.assertEqual(op["kind"], "")
        self.assertFalse(op["dismissed"])
        self.assertIsNone(op["dismissal_id"])
        self.assertEqual(op["caption_page"], 0)
        self.assertEqual(op["key_page"], 1)
        self.assertEqual(op["end_page"], 1)
        self.assertEqual(op["page_count"], 2)
        self.assertEqual(op["caption_detection_id"], caption.pk)
        self.assertEqual(op["key_detection_id"], key.pk)
        self.assertTrue(op["has_image"])
        # A range on the end page gives its start to the opinion that
        # ends there.
        self.assertEqual(op["first_page_number"], 101)
        self.assertEqual(op["last_page_number"], 102)
        self.assertEqual(op["start"], {"x": row.start_x, "y": row.start_y})
        self.assertNotIn("caption_bbox", op)

    def test_a_page_without_a_number_falls_back_to_its_position(self):
        scan = ScanFactory(page_count=2, start_page=50)
        one_opinion(scan)

        op = boundaries.viewer_payload(scan, {})[0]

        self.assertEqual(op["first_page_number"], 50)
        self.assertEqual(op["last_page_number"], 51)

    def test_a_range_on_the_start_page_gives_its_end(self):
        scan = ScanFactory(page_count=2)
        one_opinion(scan)

        op = boundaries.viewer_payload(scan, {0: (7, 8), 1: (9, None)})[0]

        self.assertEqual(op["first_page_number"], 8)
        self.assertEqual(op["last_page_number"], 9)

    def test_outside_rects_need_the_column_boxes(self):
        scan = ScanFactory(page_count=2)
        one_opinion(scan)
        self.assertEqual(
            boundaries.viewer_payload(scan, {})[0]["outside_rects"], []
        )

        for page in (0, 1):
            model_row(
                scan,
                label="TEXT_COLUMN",
                label_id=int(Label.TEXT_COLUMN),
                page_index=page,
                x0=100,
                y0=100,
                x1=800,
                y1=2100,
                img_width=IMG_W,
                img_height=IMG_H,
            )
            model_row(
                scan,
                label="TEXT_COLUMN",
                label_id=int(Label.TEXT_COLUMN),
                page_index=page,
                x0=900,
                y0=100,
                x1=1600,
                y1=2100,
                img_width=IMG_W,
                img_height=IMG_H,
            )

        rects = boundaries.viewer_payload(scan, {})[0]["outside_rects"]

        # The caption sits in the left column (x0 100 px): the mask on
        # the first page covers the left column above it. The key sits
        # in the right column (x0 900 px): the mask on the last page
        # covers the right column below it.
        self.assertEqual({r["page_index"] for r in rects}, {0, 1})
        first = [r for r in rects if r["page_index"] == 0][0]
        self.assertLess(first["y0"], first["y1"])
        self.assertLess(first["x1"], 306.0)
        last = [r for r in rects if r["page_index"] == 1][0]
        self.assertGreater(last["x0"], 306.0)

    def test_live_only_leaves_the_dismissed_rows_out(self):
        user = UserFactory()
        scan = ScanFactory(page_count=2)
        row = OpinionBoundaryFactory(scan=scan)
        boundaries.dismiss(scan, row, user)

        self.assertEqual(len(boundaries.viewer_payload(scan, {})), 1)
        self.assertEqual(
            boundaries.viewer_payload(scan, {}, live_only=True), []
        )

    def test_has_live_reads_the_dismissals_and_the_additions(self):
        user = UserFactory()
        scan = ScanFactory(page_count=3)
        self.assertFalse(boundaries.has_live(scan))
        row = OpinionBoundaryFactory(scan=scan)
        self.assertTrue(boundaries.has_live(scan))
        boundaries.dismiss(scan, row, user)
        self.assertFalse(boundaries.has_live(scan))
        added = boundaries.add(scan, (0, 1.0, 1.0), (2, 1.0, 1.0), user, None)
        self.assertTrue(boundaries.has_live(scan))
        boundaries.dismiss(scan, added, user)
        self.assertFalse(boundaries.has_live(scan))

    def test_no_rows_is_an_empty_list_with_no_lookup(self):
        scan = ScanFactory(page_count=2)
        with self.assertNumQueries(1):
            self.assertEqual(boundaries.viewer_payload(scan, {}), [])


class TestEndpoints(TestCase):
    def setUp(self):
        self.user = UserFactory()
        self.client.force_login(self.user)
        self.scan = ScanFactory(page_count=3, uploaded_by=self.user)
        self.caption, self.key, self.row = one_opinion(self.scan)

    def _post(self, path, body):
        return self.client.post(
            f"/scans/{self.scan.pk}/boundaries/{path}/",
            body,
            content_type="application/json",
        )

    def test_the_json_endpoint_reads_the_rows(self):
        response = self.client.get(f"/scans/{self.scan.pk}/opinions-json/")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual([op["id"] for op in data], [self.row.pk])
        self.assertEqual(data[0]["caption_page"], 0)

    def test_dismiss_then_restore(self):
        response = self._post("dismiss", {"boundary_id": self.row.pk})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "ok")
        self.assertIsNotNone(data["dismissal_id"])
        self.assertFalse(data["withdrawn"])
        self.row.refresh_from_db()
        self.assertTrue(self.row.is_dismissed)
        self.assertTrue(
            self.client.get(f"/scans/{self.scan.pk}/opinions-json/").json()[0][
                "dismissed"
            ]
        )

        response = self._post("restore", {"boundary_id": self.row.pk})

        self.assertEqual(response.json()["restored"], True)
        self.row.refresh_from_db()
        self.assertFalse(self.row.is_dismissed)

    def test_a_row_of_another_scan_is_404(self):
        other = OpinionBoundaryFactory()
        response = self._post("dismiss", {"boundary_id": other.pk})
        self.assertEqual(response.status_code, 404)

    def test_add_with_a_replaced_row(self):
        other_key = key_row(self.scan, 2)

        response = self._post(
            "add",
            {
                "start": {"detection_id": self.caption.pk},
                "end": {"detection_id": other_key.pk},
                "replaces": self.row.pk,
            },
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        row = OpinionBoundary.objects.get(pk=data["boundary_id"])
        self.assertEqual(row.kind, OpinionBoundary.Kind.ADD)
        self.assertEqual(row.replaces_id, data["dismissal_id"])
        self.assertEqual(row.author, self.user)
        self.row.refresh_from_db()
        self.assertTrue(self.row.is_dismissed)

    def test_add_replacing_a_curator_addition(self):
        other_key = key_row(self.scan, 2)
        first = boundaries.add(
            self.scan,
            self.caption,
            other_key,
            self.user,
            None,
            replaces=self.row,
        )

        response = self._post(
            "add",
            {
                "start": {"detection_id": self.caption.pk},
                "end": {"detection_id": self.key.pk},
                "replaces": first.pk,
            },
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["dismissal_id"], first.replaces_id)
        first.refresh_from_db()
        self.assertIsNotNone(first.withdrawn_at)
        payload = self.client.get(
            f"/scans/{self.scan.pk}/opinions-json/"
        ).json()
        self.assertEqual([op["id"] for op in payload], [data["boundary_id"]])

    def test_add_from_points(self):
        response = self._post(
            "add",
            {
                "start": {"page_index": 0, "x": 10, "y": 20},
                "end": {"page_index": 2, "x": 500, "y": 700},
            },
        )
        self.assertEqual(response.status_code, 200)

    def test_a_dismissal_of_an_addition_withdraws_it(self):
        row = boundaries.add(
            self.scan, (0, 1.0, 1.0), (2, 1.0, 1.0), self.user, None
        )

        response = self._post("dismiss", {"boundary_id": row.pk})

        self.assertTrue(response.json()["withdrawn"])
        row.refresh_from_db()
        self.assertIsNotNone(row.withdrawn_at)

    def test_a_malformed_anchor_is_400(self):
        response = self._post(
            "add", {"start": {"page_index": 0}, "end": {"page_index": 1}}
        )
        self.assertEqual(response.status_code, 400)

    def test_an_end_before_the_start_is_400(self):
        response = self._post(
            "add",
            {
                "start": {"page_index": 2, "x": 1, "y": 1},
                "end": {"page_index": 0, "x": 1, "y": 1},
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_a_page_outside_the_map_is_409(self):
        response = self._post(
            "add",
            {
                "start": {"page_index": 0, "x": 1, "y": 1},
                "end": {"page_index": 40, "x": 1, "y": 1},
            },
        )
        # No run: the original's space, where every index has an
        # address. Give the scan a run and a map without the page.
        self.assertEqual(response.status_code, 200)
        from unittest.mock import patch

        from scanning import detections

        run = glued_run(self.scan)
        with patch.object(detections, "measured_run", return_value=run):
            response = self._post(
                "add",
                {
                    "start": {"page_index": 0, "x": 1, "y": 1},
                    "end": {"page_index": 40, "x": 1, "y": 1},
                },
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["status"], "error")

    def test_a_dismissal_of_an_unaddressed_boundary_is_409(self):
        OpinionBoundary.objects.filter(pk=self.row.pk).update(
            end_source_page=None
        )
        response = self._post("dismiss", {"boundary_id": self.row.pk})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["status"], "error")

    def test_login_is_required(self):
        self.client.logout()
        response = self._post("dismiss", {"boundary_id": self.row.pk})
        self.assertEqual(response.status_code, 302)
