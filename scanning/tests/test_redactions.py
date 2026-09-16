"""Tests for ``scanning.redactions`` (issue #240, PR B).

The computed rows the compute writes, the curator's decisions about
them, and the resolution after a compute. The compute around them is
tested in ``test_yolo_apply``.
"""

from types import SimpleNamespace

from django.test import TestCase

from scanning import redactions
from scanning.factories import ScanFactory, UserFactory
from scanning.models import PageEdit, Redaction
from scanning.tests.test_yolo_apply import glued_run, identity_map


def computed(scan, **fields) -> Redaction:
    """Store one computed box, in points.

    :param scan: The scan.
    :param fields: Overrides.
    :returns: The row.
    """
    values = {
        "scan": scan,
        "origin": Redaction.Origin.COMPUTED,
        "rect_type": "headnote",
        "fill": "black",
        "x0": 50.0,
        "y0": 100.0,
        "x1": 150.0,
        "y1": 200.0,
        "source_page": 1,
        "source_fingerprint": scan.source_fingerprint,
        "page_index": 0,
    }
    values.update(fields)
    return Redaction.objects.create(**values)


def pages(*indexes, scale=0.5):
    """Fake blackletter pages with one scale.

    :param indexes: The page indexes.
    :param scale: Points per pixel.
    :returns: The list.
    """
    return [
        SimpleNamespace(index=i, scale_x=scale, scale_y=scale) for i in indexes
    ]


class TestWriteComputed(TestCase):
    def setUp(self):
        self.scan = ScanFactory(page_count=3, source_fingerprint="10:3")

    def test_converts_pixels_to_points_and_addresses_each_row(self):
        rects = [
            {
                "page_index": 1,
                "rects": [
                    {
                        "x0": 100,
                        "y0": 200,
                        "x1": 300,
                        "y1": 400,
                        "fill": "black",
                        "type": "headnote",
                    },
                    {
                        "x0": 10,
                        "y0": 10,
                        "x1": 10,
                        "y1": 20,
                        "fill": "black",
                        "type": "KEY_ICON",
                    },
                ],
            }
        ]
        margins = [
            {
                "page_index": 2,
                "rects": [{"x0": 0, "y0": 0, "x1": 20, "y1": 50}],
            }
        ]

        written = redactions.write_computed(
            self.scan, None, 3, rects, margins, pages(1, 2)
        )

        self.assertEqual(written, 2)
        head = Redaction.objects.get(scan=self.scan, rect_type="headnote")
        self.assertEqual(head.bbox, [50.0, 100.0, 150.0, 200.0])
        self.assertEqual(
            (head.source_edit, head.source_page, head.page_index), (None, 2, 1)
        )
        self.assertEqual(head.detect_run, 3)
        self.assertEqual(head.source_fingerprint, "10:3")
        self.assertIsNone(head.apply_run)
        margin = Redaction.objects.get(scan=self.scan, rect_type="margin")
        self.assertEqual(margin.fill, "white")
        self.assertEqual(margin.bbox, [0.0, 0.0, 20.0, 50.0])
        self.assertEqual((margin.source_page, margin.page_index), (3, 2))

    def test_replaces_the_computed_rows_and_keeps_the_human_ones(self):
        old = computed(self.scan)
        drawn = redactions.add(
            self.scan, 0, [1.0, 2.0, 3.0, 4.0], "white", None
        )

        redactions.write_computed(self.scan, None, 1, [], [], [])

        self.assertFalse(Redaction.objects.filter(pk=old.pk).exists())
        self.assertTrue(Redaction.objects.filter(pk=drawn.pk).exists())

    def test_addresses_through_the_run_map(self):
        run = glued_run(
            self.scan,
            page_map={
                **identity_map(3),
                "final_page_count": 2,
                "deleted_pages": [1],
                "pages": [
                    {
                        "final_page": 1,
                        "source": {"kind": "original", "pdf_page": 2},
                    },
                    {
                        "final_page": 2,
                        "source": {"kind": "original", "pdf_page": 3},
                    },
                ],
            },
        )
        rects = [
            {
                "page_index": 0,
                "rects": [
                    {
                        "x0": 0,
                        "y0": 0,
                        "x1": 10,
                        "y1": 10,
                        "fill": "black",
                        "type": "t",
                    }
                ],
            }
        ]

        redactions.write_computed(self.scan, run, 1, rects, [], pages(0))

        row = Redaction.objects.get(scan=self.scan)
        self.assertEqual((row.source_page, row.page_index), (2, 0))
        self.assertEqual(row.apply_run, run)

    def test_a_page_with_no_scale_is_refused(self):
        """Pixels stored as points would be a small box in the wrong
        place, with no error (PR #290 review)."""
        rects = [
            {
                "page_index": 1,
                "rects": [
                    {
                        "x0": 0,
                        "y0": 0,
                        "x1": 10,
                        "y1": 10,
                        "fill": "black",
                        "type": "t",
                    }
                ],
            }
        ]

        with self.assertRaises(redactions.UnaddressableRedaction):
            redactions.write_computed(self.scan, None, 1, rects, [], pages(0))

        self.assertEqual(Redaction.objects.count(), 0)

    def test_a_page_outside_the_map_is_refused(self):
        run = glued_run(self.scan)
        rects = [
            {
                "page_index": 9,
                "rects": [
                    {
                        "x0": 0,
                        "y0": 0,
                        "x1": 10,
                        "y1": 10,
                        "fill": "black",
                        "type": "t",
                    }
                ],
            }
        ]

        with self.assertRaises(redactions.UnaddressableRedaction):
            redactions.write_computed(self.scan, run, 1, rects, [], pages(9))


class TestResolve(TestCase):
    def setUp(self):
        self.scan = ScanFactory(page_count=2, source_fingerprint="10:2")

    def _dismiss(self, **fields) -> Redaction:
        row = computed(self.scan, **fields)
        decision = redactions.dismiss(self.scan, row, None)
        row.delete()
        return decision

    def test_no_dismiss_reads_no_row(self):
        computed(self.scan)
        with self.assertNumQueries(2):
            self.assertEqual(redactions.resolve(self.scan), (0, []))

    def test_lands_on_the_same_box(self):
        decision = self._dismiss()
        new = computed(self.scan)

        landed, stale = redactions.resolve(self.scan)

        self.assertEqual((landed, stale), (1, []))
        new.refresh_from_db()
        self.assertEqual(new.decision, decision)
        self.assertNotIn(
            new.pk,
            [
                r["id"]
                for e in redactions.visible_by_page(self.scan)
                for r in e["rects"]
            ],
        )

    def test_lands_on_a_box_that_moved_a_little(self):
        self._dismiss()
        new = computed(self.scan, x0=48.0, x1=152.0)

        landed, _ = redactions.resolve(self.scan)

        self.assertEqual(landed, 1)
        new.refresh_from_db()
        self.assertIsNotNone(new.decision)

    def test_needs_the_same_page_and_type(self):
        self._dismiss()
        other_page = computed(self.scan, page_index=1, source_page=2)
        other_type = computed(self.scan, rect_type="margin", fill="white")

        with self.assertLogs("scanning.redactions", level="WARNING"):
            landed, stale = redactions.resolve(self.scan)

        self.assertEqual((landed, len(stale)), (0, 1))
        other_page.refresh_from_db()
        other_type.refresh_from_db()
        self.assertIsNone(other_page.decision)
        self.assertIsNone(other_type.decision)

    def test_a_box_below_the_threshold_is_stale(self):
        decision = self._dismiss()
        computed(self.scan, x0=120.0, y0=180.0, x1=220.0, y1=280.0)

        with self.assertLogs("scanning.redactions", level="WARNING"):
            landed, stale = redactions.resolve(self.scan)

        self.assertEqual((landed, stale), (0, [decision]))
        decision.refresh_from_db()
        self.assertIsNone(decision.withdrawn_at)

    def test_another_original_is_stale(self):
        decision = self._dismiss()
        Redaction.objects.filter(pk=decision.pk).update(
            source_fingerprint="99:2"
        )
        new = computed(self.scan)

        with self.assertLogs("scanning.redactions", level="WARNING"):
            redactions.resolve(self.scan)

        new.refresh_from_db()
        self.assertIsNone(new.decision)

    def test_each_box_is_taken_once(self):
        self._dismiss()
        self._dismiss()
        one = computed(self.scan)
        two = computed(self.scan)

        landed, stale = redactions.resolve(self.scan)

        self.assertEqual((landed, stale), (2, []))
        one.refresh_from_db()
        two.refresh_from_db()
        self.assertIsNotNone(one.decision)
        self.assertIsNotNone(two.decision)
        self.assertNotEqual(one.decision, two.decision)


class TestDecisions(TestCase):
    def setUp(self):
        self.scan = ScanFactory(page_count=2, source_fingerprint="10:2")
        self.user = UserFactory()

    def test_add_is_addressed_and_visible(self):
        row = redactions.add(
            self.scan, 1, [1.0, 2.0, 3.5, 4.25], "white", self.user
        )

        self.assertEqual(row.origin, Redaction.Origin.HUMAN)
        self.assertEqual(row.kind, Redaction.Kind.ADD)
        self.assertEqual(row.rect_type, "manual")
        self.assertEqual(row.bbox, [1.0, 2.0, 3.5, 4.2])
        self.assertEqual((row.source_page, row.page_index), (2, 1))
        self.assertEqual(row.author, self.user)
        visible = redactions.visible_by_page(self.scan)
        self.assertEqual(visible[0]["page_index"], 1)
        self.assertEqual(visible[0]["rects"][0]["origin"], "human")

    def test_add_refuses_a_page_the_map_lacks(self):
        run = glued_run(self.scan)
        with self.assertLogs("scanning.redactions", level="WARNING"):
            with self.assertRaises(redactions.UnaddressableRedaction):
                redactions.add(
                    self.scan,
                    7,
                    [1.0, 2.0, 3.0, 4.0],
                    "black",
                    self.user,
                    run=run,
                )
        self.assertEqual(Redaction.objects.count(), 0)

    def test_add_refuses_an_empty_box(self):
        with self.assertRaises(ValueError):
            redactions.add(
                self.scan, 0, [3.0, 2.0, 1.0, 4.0], "black", self.user
            )

    def test_dismiss_of_a_computed_box_hides_it(self):
        row = computed(self.scan)

        decision = redactions.dismiss(self.scan, row, self.user)

        row.refresh_from_db()
        self.assertEqual(row.decision, decision)
        self.assertEqual(decision.kind, Redaction.Kind.DISMISS)
        self.assertEqual(decision.target_bbox, [50.0, 100.0, 150.0, 200.0])
        self.assertIsNone(decision.bbox)
        self.assertEqual(decision.rect_type, "headnote")
        self.assertEqual(redactions.visible_by_page(self.scan), [])
        self.assertEqual(
            redactions.dismiss(self.scan, row, self.user), decision
        )
        self.assertEqual(Redaction.objects.human().count(), 1)

    def test_dismiss_of_a_drawn_box_withdraws_it(self):
        row = redactions.add(
            self.scan, 0, [1.0, 2.0, 3.0, 4.0], "black", self.user
        )

        self.assertIsNone(redactions.dismiss(self.scan, row, self.user))

        row.refresh_from_db()
        self.assertIsNotNone(row.withdrawn_at)
        self.assertEqual(row.withdrawn_by, self.user)
        self.assertEqual(redactions.visible_by_page(self.scan), [])

    def test_move_of_a_computed_box_is_a_dismiss_plus_an_add(self):
        row = computed(self.scan)

        holder = redactions.move(
            self.scan, row, [60.0, 110.0, 160.0, 210.0], self.user
        )

        row.refresh_from_db()
        self.assertIsNotNone(row.decision)
        self.assertEqual(holder.kind, Redaction.Kind.ADD)
        self.assertEqual(holder.replaces, row.decision)
        self.assertEqual(holder.rect_type, "headnote")
        self.assertEqual(holder.fill, "black")
        self.assertEqual(holder.bbox, [60.0, 110.0, 160.0, 210.0])
        ids = [
            r["id"]
            for e in redactions.visible_by_page(self.scan)
            for r in e["rects"]
        ]
        self.assertEqual(ids, [holder.pk])

    def test_a_second_move_of_the_same_computed_box_writes_one_box(self):
        """Two drags in flight before the first answer both name the
        computed row (PR #290 review): the second writes on the box the
        first drew, and one box paints, at the second position."""
        row = computed(self.scan)
        first = redactions.move(
            self.scan, row, [60.0, 110.0, 160.0, 210.0], self.user
        )

        second = redactions.move(
            self.scan, row, [70.0, 120.0, 170.0, 220.0], self.user
        )

        self.assertEqual(second.pk, first.pk)
        self.assertEqual(second.bbox, [70.0, 120.0, 170.0, 220.0])
        ids = [
            r["id"]
            for e in redactions.visible_by_page(self.scan)
            for r in e["rects"]
        ]
        self.assertEqual(ids, [first.pk])
        self.assertEqual(Redaction.objects.human().count(), 2)

    def test_the_database_refuses_a_second_standing_replacement(self):
        from django.db import IntegrityError

        row = computed(self.scan)
        first = redactions.move(
            self.scan, row, [60.0, 110.0, 160.0, 210.0], self.user
        )

        with self.assertRaises(IntegrityError):
            Redaction.objects.create(
                scan=self.scan,
                origin=Redaction.Origin.HUMAN,
                kind=Redaction.Kind.ADD,
                rect_type="headnote",
                fill="black",
                x0=1.0,
                y0=2.0,
                x1=3.0,
                y1=4.0,
                replaces=first.replaces,
                source_page=1,
                page_index=0,
            )

    def test_move_of_a_drawn_box_is_in_place(self):
        row = redactions.add(
            self.scan, 0, [1.0, 2.0, 3.0, 4.0], "black", self.user
        )

        holder = redactions.move(
            self.scan, row, [2.0, 3.0, 4.0, 5.0], self.user
        )

        self.assertEqual(holder.pk, row.pk)
        self.assertEqual(holder.bbox, [2.0, 3.0, 4.0, 5.0])
        self.assertEqual(Redaction.objects.count(), 1)

    def test_deleting_a_moved_box_deletes_it_and_leaves_the_computed_one_out(
        self,
    ):
        """Delete is not undo (PR #290 review): the computed box the move
        replaced must not come back."""
        row = computed(self.scan)
        holder = redactions.move(
            self.scan, row, [60.0, 110.0, 160.0, 210.0], self.user
        )

        self.assertIsNone(redactions.dismiss(self.scan, holder, self.user))

        holder.refresh_from_db()
        row.refresh_from_db()
        self.assertIsNotNone(holder.withdrawn_at)
        self.assertIsNotNone(row.decision)
        self.assertIsNone(holder.replaces.withdrawn_at)
        self.assertEqual(redactions.visible_by_page(self.scan), [])

    def test_undo_move_gives_the_computed_box_back(self):
        row = computed(self.scan)
        holder = redactions.move(
            self.scan, row, [60.0, 110.0, 160.0, 210.0], self.user
        )

        count = redactions.undo_move(self.scan, holder, self.user)

        self.assertEqual(count, 2)
        row.refresh_from_db()
        holder.refresh_from_db()
        self.assertIsNone(row.decision)
        self.assertIsNotNone(holder.withdrawn_at)
        self.assertIsNotNone(holder.replaces.withdrawn_at)
        ids = [
            r["id"]
            for e in redactions.visible_by_page(self.scan)
            for r in e["rects"]
        ]
        self.assertEqual(ids, [row.pk])
        self.assertEqual(redactions.undo_move(self.scan, holder, self.user), 0)
        drawn = redactions.add(
            self.scan, 0, [1.0, 2.0, 3.0, 4.0], "black", None
        )
        self.assertEqual(redactions.undo_move(self.scan, drawn, self.user), 0)

    def test_restore_after_a_move_shows_one_box(self):
        """The computed box comes back alone (PR #290 review): the moved
        copy that replaced it is withdrawn with the dismiss."""
        row = computed(self.scan)
        holder = redactions.move(
            self.scan, row, [60.0, 110.0, 160.0, 210.0], self.user
        )

        self.assertTrue(redactions.restore(self.scan, row, self.user))

        holder.refresh_from_db()
        self.assertIsNotNone(holder.withdrawn_at)
        ids = [
            r["id"]
            for e in redactions.visible_by_page(self.scan)
            for r in e["rects"]
        ]
        self.assertEqual(ids, [row.pk])

    def test_restore_withdraws_the_dismiss(self):
        row = computed(self.scan)
        redactions.dismiss(self.scan, row, self.user)

        self.assertTrue(redactions.restore(self.scan, row, self.user))

        row.refresh_from_db()
        self.assertIsNone(row.decision)
        self.assertFalse(redactions.restore(self.scan, row, self.user))

    def test_visible_by_page_groups_and_orders(self):
        computed(self.scan, page_index=1, source_page=2, y0=300.0, y1=400.0)
        computed(
            self.scan,
            page_index=1,
            source_page=2,
            y0=10.0,
            y1=20.0,
            rect_type="margin",
            fill="white",
        )
        computed(self.scan)

        visible = redactions.visible_by_page(self.scan)

        self.assertEqual([e["page_index"] for e in visible], [0, 1])
        self.assertEqual(
            [r["rect_type"] for r in visible[1]["rects"]],
            ["margin", "headnote"],
        )
        self.assertEqual(
            set(visible[0]["rects"][0]),
            {"id", "x0", "y0", "x1", "y1", "fill", "rect_type", "origin"},
        )

    def test_an_edit_page_is_its_own_address(self):
        edit = PageEdit.objects.create(
            scan=self.scan,
            kind=PageEdit.Kind.INSERT_PAGE,
            anchor_pdf_page=1,
            source_fingerprint="10:2",
        )
        computed(self.scan, source_edit=edit, source_page=1, page_index=1)
        row = Redaction.objects.get(scan=self.scan)
        decision = redactions.dismiss(self.scan, row, self.user)
        self.assertEqual(decision.source_edit, edit)
