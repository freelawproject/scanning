"""Tests for the text redaction fit (issue #279).

``text_fit`` makes a text redaction box as narrow as the dots.mocr
cells under it. Tested here: the reader of the glued OCR document, the
one rule and its guards, the compute's caller over blackletter's
pixel rects, the backfill's caller over the stored rows in points, and
the management command.
"""

from io import StringIO
from types import SimpleNamespace

from django.core.management import call_command
from django.test import TestCase

from scanning import text_fit
from scanning.factories import ScanFactory, UserFactory
from scanning.models import Redaction, Status

#: The page of every fixture: 1700 x 2200 pixels of a 200 dpi render,
#: which is 612 x 792 points.
WIDTH, HEIGHT = 1700, 2200
POINTS_W, POINTS_H = 612.0, 792.0

#: The fallback column split blackletter applies with no column bounds:
#: 3 % of side padding and a 2 % gutter on each side of the middle.
LEFT_COLUMN = (51.0, 816.0)
RIGHT_COLUMN = (884.0, 1649.0)

#: Where the text of each column really is, in the render pixels.
LEFT_TEXT = (180.0, 780.0)
RIGHT_TEXT = (920.0, 1520.0)


def cell(x0, y0, x1, y1, text="a headnote"):
    """One dots.mocr cell.

    :param x0: Left, in render pixels.
    :param y0: Top, in render pixels.
    :param x1: Right, in render pixels.
    :param y1: Bottom, in render pixels.
    :param text: The cell's text.
    :returns: The cell dict.
    """
    return {"bbox": [x0, y0, x1, y1], "category": "Text", "text": text}


def ocr_document(*pages):
    """A glued OCR document holding ``pages``.

    :param pages: One ``(page_index, cells)`` pair per page.
    :returns: The document dict.
    """
    return {
        "schema_version": 1,
        "pages": [
            {
                "page_index": index,
                "pdf_page": index + 1,
                "origin_width": WIDTH,
                "origin_height": HEIGHT,
                "cells": cells,
            }
            for index, cells in pages
        ],
    }


def two_column_page(index=0):
    """A page whose two columns each hold one block of text.

    :param index: The page index.
    :returns: The ``(page_index, cells)`` pair.
    """
    return (
        index,
        [
            cell(LEFT_TEXT[0], 300, LEFT_TEXT[1], 900),
            cell(RIGHT_TEXT[0], 300, RIGHT_TEXT[1], 900),
        ],
    )


def bl_pages(*indexes):
    """Fake blackletter pages of the fixture size.

    :param indexes: The page indexes.
    :returns: The list.
    """
    return [
        SimpleNamespace(
            index=index,
            img_width=WIDTH,
            img_height=HEIGHT,
            pdf_width=POINTS_W,
            pdf_height=POINTS_H,
        )
        for index in indexes
    ]


def rect(x0, y0, x1, y1, rtype="headnote", page_index=0):
    """One entry of blackletter's rects, in render pixels.

    :param x0: Left.
    :param y0: Top.
    :param x1: Right.
    :param y1: Bottom.
    :param rtype: The rect type.
    :param page_index: The page.
    :returns: The one-page entry.
    """
    return {
        "page_index": page_index,
        "rects": [
            {
                "x0": x0,
                "y0": y0,
                "x1": x1,
                "y1": y1,
                "fill": "black",
                "type": rtype,
            }
        ],
    }


def computed(scan, **fields):
    """Store one computed redaction row, in points.

    :param scan: The scan.
    :param fields: Overrides.
    :returns: The row.
    """
    values = {
        "scan": scan,
        "origin": Redaction.Origin.COMPUTED,
        "rect_type": "headnote",
        "fill": "black",
        "x0": LEFT_COLUMN[0] * POINTS_W / WIDTH,
        "y0": 400 * POINTS_H / HEIGHT,
        "x1": LEFT_COLUMN[1] * POINTS_W / WIDTH,
        "y1": 800 * POINTS_H / HEIGHT,
        "source_page": 1,
        "source_fingerprint": scan.source_fingerprint,
        "page_index": 0,
    }
    values.update(fields)
    return Redaction.objects.create(**values)


class TestPageCells(TestCase):
    """The reader of a glued OCR document."""

    def test_reads_the_boxes_of_each_page(self):
        cells = text_fit.page_cells(ocr_document(two_column_page(0)))
        self.assertEqual(list(cells), [0])
        self.assertEqual(cells[0].width, WIDTH)
        self.assertEqual(cells[0].height, HEIGHT)
        self.assertEqual(len(cells[0].boxes), 2)

    def test_skips_a_page_with_no_cell(self):
        document = ocr_document((0, []), (1, [cell(10, 10, 20, 20)]))
        document["pages"][0]["error"] = "not read"
        self.assertEqual(list(text_fit.page_cells(document)), [1])

    def test_skips_a_page_with_no_render_size(self):
        document = ocr_document(two_column_page(0))
        document["pages"][0]["origin_width"] = 0
        self.assertEqual(text_fit.page_cells(document), {})

    def test_skips_a_broken_cell(self):
        document = ocr_document(
            (
                0,
                [
                    {"bbox": [10, 10, 5, 20]},
                    {"bbox": "not a box"},
                    {"text": "no box at all"},
                    cell(10, 10, 20, 20),
                ],
            )
        )
        self.assertEqual(len(text_fit.page_cells(document)[0].boxes), 1)

    def test_reads_nothing_from_a_non_document(self):
        self.assertEqual(text_fit.page_cells(None), {})
        self.assertEqual(text_fit.page_cells({"pages": None}), {})


class TestFitRects(TestCase):
    """The compute's caller, over blackletter's pixel rects."""

    def setUp(self):
        self.cells = text_fit.page_cells(ocr_document(two_column_page(0)))

    def fit(self, rects):
        """Run the fit and return the one rect it read.

        :param rects: blackletter's rects.
        :returns: ``(fitted count, the rect)``.
        """
        count = text_fit.fit_rects(rects, self.cells, bl_pages(0))
        return count, rects[0]["rects"][0]

    def test_fits_a_left_column_box_to_its_text(self):
        count, box = self.fit([rect(LEFT_COLUMN[0], 400, LEFT_COLUMN[1], 800)])
        self.assertEqual(count, 1)
        # The pad is 2 points, which is 5.6 pixels of this render.
        self.assertAlmostEqual(box["x0"], LEFT_TEXT[0] - 5.6, delta=0.2)
        self.assertAlmostEqual(box["x1"], LEFT_TEXT[1] + 5.6, delta=0.2)

    def test_fits_a_right_column_box_to_its_text(self):
        count, box = self.fit(
            [rect(RIGHT_COLUMN[0], 400, RIGHT_COLUMN[1], 800)]
        )
        self.assertEqual(count, 1)
        self.assertAlmostEqual(box["x0"], RIGHT_TEXT[0] - 5.6, delta=0.2)
        self.assertAlmostEqual(box["x1"], RIGHT_TEXT[1] + 5.6, delta=0.2)

    def test_leaves_the_vertical_limits_alone(self):
        _count, box = self.fit(
            [rect(LEFT_COLUMN[0], 400, LEFT_COLUMN[1], 800)]
        )
        self.assertEqual((box["y0"], box["y1"]), (400, 800))

    def test_reads_no_cell_that_lies_above_the_box(self):
        # The box covers the bottom of the column; the only cell there
        # is narrow, and the wide cell above it must not be read.
        cells = text_fit.page_cells(
            ocr_document(
                (
                    0,
                    [
                        cell(100, 100, 1600, 500),
                        cell(200, 600, 700, 900),
                    ],
                )
            )
        )
        rects = [rect(LEFT_COLUMN[0], 620, LEFT_COLUMN[1], 880)]
        text_fit.fit_rects(rects, cells, bl_pages(0))
        box = rects[0]["rects"][0]
        self.assertAlmostEqual(box["x0"], 200 - 5.6, delta=0.2)
        self.assertAlmostEqual(box["x1"], 700 + 5.6, delta=0.2)

    def test_reads_no_cell_of_the_facing_column(self):
        # A box of the left column takes nothing from the right one,
        # even though both cells overlap it vertically.
        _count, box = self.fit(
            [rect(LEFT_COLUMN[0], 400, LEFT_COLUMN[1], 800)]
        )
        self.assertLess(box["x1"], RIGHT_TEXT[0])

    def test_a_box_no_cell_reaches_does_not_move(self):
        rects = [rect(LEFT_COLUMN[0], 1500, LEFT_COLUMN[1], 1800)]
        count = text_fit.fit_rects(rects, self.cells, bl_pages(0))
        self.assertEqual(count, 0)
        self.assertEqual(rects[0]["rects"][0]["x0"], LEFT_COLUMN[0])

    def test_never_makes_a_box_wider(self):
        # A box already inside the text keeps its own limits.
        rects = [rect(300.0, 400, 600.0, 800)]
        text_fit.fit_rects(rects, self.cells, bl_pages(0))
        self.assertEqual(
            (rects[0]["rects"][0]["x0"], rects[0]["rects"][0]["x1"]),
            (300.0, 600.0),
        )

    def test_a_page_with_no_cells_does_not_move(self):
        rects = [rect(LEFT_COLUMN[0], 400, LEFT_COLUMN[1], 800, page_index=1)]
        count = text_fit.fit_rects(rects, self.cells, bl_pages(0, 1))
        self.assertEqual(count, 0)

    def test_only_a_text_type_moves(self):
        for rtype in ("DIVIDER", "HEADNOTE_BRACKET", "PAGE_HEADER", "margin"):
            with self.subTest(rtype=rtype):
                rects = [
                    rect(LEFT_COLUMN[0], 400, LEFT_COLUMN[1], 800, rtype=rtype)
                ]
                count = text_fit.fit_rects(rects, self.cells, bl_pages(0))
                self.assertEqual(count, 0)

    def test_an_editorial_box_moves(self):
        rects = [
            rect(LEFT_COLUMN[0], 400, LEFT_COLUMN[1], 800, rtype="EDITORIAL")
        ]
        self.assertEqual(text_fit.fit_rects(rects, self.cells, bl_pages(0)), 1)

    def test_applies_the_scale_of_two_different_renders(self):
        # The cells were measured in a render 1700 pixels wide; the
        # detections in one 1650 wide. The fit must cross the two.
        pages = bl_pages(0)
        pages[0].img_width = 1650
        pages[0].img_height = 2135
        rects = [rect(50.0, 390, 792.0, 780)]
        text_fit.fit_rects(rects, self.cells, pages)
        ratio = 1650 / WIDTH
        box = rects[0]["rects"][0]
        self.assertAlmostEqual(
            box["x0"], LEFT_TEXT[0] * ratio - 5.4, delta=0.3
        )
        self.assertAlmostEqual(
            box["x1"], LEFT_TEXT[1] * ratio + 5.4, delta=0.3
        )

    def test_refuses_a_fit_below_the_floor(self):
        # One small cell under a full-column box would leave most of
        # the headnote in the deliverable.
        cells = text_fit.page_cells(
            ocr_document((0, [cell(300, 400, 380, 500)]))
        )
        rects = [rect(LEFT_COLUMN[0], 390, LEFT_COLUMN[1], 800)]
        count = text_fit.fit_rects(rects, cells, bl_pages(0))
        self.assertEqual(count, 0)
        self.assertEqual(rects[0]["rects"][0]["x0"], LEFT_COLUMN[0])

    def test_no_cells_at_all_is_a_no_op(self):
        rects = [rect(LEFT_COLUMN[0], 400, LEFT_COLUMN[1], 800)]
        self.assertEqual(text_fit.fit_rects(rects, {}, bl_pages(0)), 0)


class TestFitSpanCounts(TestCase):
    """The one rule names why a box did not move."""

    def test_unreached_when_no_cell_overlaps(self):
        cells = text_fit.page_cells(ocr_document(two_column_page(0)))[0]
        fit = text_fit.fit_span((0.0, 0.9, 0.5, 0.95), cells, 0.0)
        self.assertEqual((fit.span, fit.reason), (None, "unreached"))

    def test_refused_when_the_answer_is_too_narrow(self):
        cells = text_fit.page_cells(
            ocr_document((0, [cell(300, 400, 380, 500)]))
        )[0]
        fit = text_fit.fit_span((0.03, 0.18, 0.48, 0.36), cells, 0.0)
        self.assertEqual((fit.span, fit.reason), (None, "refused"))


class TestFitRows(TestCase):
    """The backfill's caller, over the stored rows in points."""

    def setUp(self):
        self.scan = ScanFactory(page_count=2, source_fingerprint="10:2")
        self.cells = text_fit.page_cells(ocr_document(two_column_page(0)))

    def test_fits_a_standing_row(self):
        row = computed(self.scan)
        counts = text_fit.fit_rows(self.scan, self.cells)
        row.refresh_from_db()
        self.assertEqual((counts.read, counts.fitted), (1, 1))
        self.assertAlmostEqual(
            row.x0, LEFT_TEXT[0] * POINTS_W / WIDTH - 2.0, delta=0.2
        )
        self.assertAlmostEqual(
            row.x1, LEFT_TEXT[1] * POINTS_W / WIDTH + 2.0, delta=0.2
        )

    def test_leaves_the_vertical_limits_alone(self):
        row = computed(self.scan)
        before = (row.y0, row.y1)
        text_fit.fit_rows(self.scan, self.cells)
        row.refresh_from_db()
        self.assertEqual((row.y0, row.y1), before)

    def test_is_idempotent(self):
        computed(self.scan)
        text_fit.fit_rows(self.scan, self.cells)
        second = text_fit.fit_rows(self.scan, self.cells)
        self.assertEqual(second.fitted, 0)

    def test_leaves_a_dismissed_row_alone(self):
        row = computed(self.scan)
        dismiss = Redaction.objects.create(
            scan=self.scan,
            origin=Redaction.Origin.HUMAN,
            kind=Redaction.Kind.DISMISS,
            rect_type="headnote",
            fill="black",
            source_page=1,
            page_index=0,
            target_x0=row.x0,
            target_y0=row.y0,
            target_x1=row.x1,
            target_y1=row.y1,
            author=UserFactory(),
        )
        Redaction.objects.filter(pk=row.pk).update(decision=dismiss)
        before = row.x0
        counts = text_fit.fit_rows(self.scan, self.cells)
        row.refresh_from_db()
        self.assertEqual(counts.read, 0)
        self.assertEqual(row.x0, before)

    def test_leaves_a_human_row_alone(self):
        row = computed(
            self.scan,
            origin=Redaction.Origin.HUMAN,
            kind=Redaction.Kind.ADD,
            rect_type=Redaction.MANUAL_TYPE,
            author=UserFactory(),
        )
        before = row.x0
        text_fit.fit_rows(self.scan, self.cells)
        row.refresh_from_db()
        self.assertEqual(row.x0, before)

    def test_leaves_a_margin_row_alone(self):
        row = computed(self.scan, rect_type=Redaction.MARGIN_TYPE)
        before = row.x0
        text_fit.fit_rows(self.scan, self.cells)
        row.refresh_from_db()
        self.assertEqual(row.x0, before)

    def test_leaves_a_page_with_no_cells_alone(self):
        row = computed(self.scan, page_index=1, source_page=2)
        before = row.x0
        text_fit.fit_rows(self.scan, self.cells)
        row.refresh_from_db()
        self.assertEqual(row.x0, before)


class TestCommand(TestCase):
    """``refit_text_redactions``."""

    def setUp(self):
        self.scan = ScanFactory(
            page_count=2,
            source_fingerprint="10:2",
            status=Status.READY_FOR_REDACTION_REVIEW,
        )
        self.row = computed(self.scan)
        self.document = ocr_document(two_column_page(0))

    def run_command(self, *args):
        """Run the command with the OCR document in place.

        :param args: The CLI arguments.
        :returns: The output.
        :rtype: str
        """
        out = StringIO()
        with self.settings(TESTING=True):
            with _cells(self.document):
                call_command(
                    "refit_text_redactions", *args, stdout=out, stderr=out
                )
        return out.getvalue()

    def test_fits_the_open_volumes(self):
        output = self.run_command()
        self.row.refresh_from_db()
        self.assertIn("1 fitted", output)
        self.assertLess(LEFT_COLUMN[0] * POINTS_W / WIDTH, self.row.x0)

    def test_a_dry_run_writes_nothing(self):
        before = self.row.x0
        output = self.run_command("--dry-run")
        self.row.refresh_from_db()
        self.assertIn("nothing written", output.lower())
        self.assertEqual(self.row.x0, before)

    def test_skips_a_closed_review_without_all(self):
        self.scan.status = Status.REDACTION_REVIEW_DONE
        self.scan.save(update_fields=["status"])
        self.assertIn("No volume to fit", self.run_command())
        self.assertIn("1 fitted", self.run_command("--all"))

    def test_takes_a_named_scan_in_any_status(self):
        self.scan.status = Status.APPROVED
        self.scan.save(update_fields=["status"])
        self.assertIn("1 fitted", self.run_command(str(self.scan.pk)))

    def test_reports_a_volume_with_no_ocr_volume(self):
        self.document = None
        self.assertIn("no OCR volume", self.run_command())


def _cells(document):
    """Patch :func:`text_fit.load_cells` to answer ``document``.

    :param document: The glued OCR document, or None for no read.
    :returns: The patch context manager.
    """
    from unittest.mock import patch

    return patch.object(
        text_fit,
        "load_cells",
        lambda scan, run: text_fit.page_cells(document),
    )
