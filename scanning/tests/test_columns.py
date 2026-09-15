"""Tests for the column gutter (issue #308).

``columns`` separates the two ``TEXT_COLUMN`` boxes of a page before
any geometry reads them. Tested here: the band the dots.mocr cells
give, the one rule and its guards, the writer over the ``Detection``
rows and the writer over a blackletter document.
"""

from types import SimpleNamespace

from blackletter.models import BBox, Label, Page
from blackletter.models import Detection as BLDetection
from django.test import TestCase

from scanning import columns, text_fit
from scanning.factories import ScanFactory
from scanning.models import Detection

#: The page of every fixture: 1700 x 2200 pixels of a 200 dpi render.
WIDTH, HEIGHT = 1700, 2200

#: Page 17 of scan 2845, the page of the issue. The model gives two
#: boxes that share an edge at the centre of the block; the cells say
#: the left column's text ends at 823 and the right column's starts at
#: 850.
LEFT_BOX = (231.1, 176.6, 824.3, 1983.5)
RIGHT_BOX = (824.3, 176.6, 1417.6, 1983.5)
LEFT_TEXT_END = 823.0
RIGHT_TEXT_START = 850.0


def cell(x0, y0, x1, y1, category="Text"):
    """One dots.mocr cell.

    :param x0: Left, in render pixels.
    :param y0: Top, in render pixels.
    :param x1: Right, in render pixels.
    :param y1: Bottom, in render pixels.
    :param category: The cell's layout category.
    :returns: The cell dict.
    """
    return {"bbox": [x0, y0, x1, y1], "category": category, "text": "a line"}


def cells_of(*cells, index=0):
    """The :class:`text_fit.PageCells` of one page holding ``cells``.

    :param cells: The cell dicts.
    :param index: The page index.
    :returns: The page's cells.
    """
    document = {
        "pages": [
            {
                "page_index": index,
                "origin_width": WIDTH,
                "origin_height": HEIGHT,
                "cells": list(cells),
            }
        ]
    }
    return text_fit.page_cells(document)[index]


def body_cells():
    """The cells of a two-column page, with a running head over the gutter.

    :returns: The cell dicts, in reading order.
    """
    return [
        cell(562, 100, 1100, 131, category="Page-header"),
        cell(249, 183, LEFT_TEXT_END, 801),
        cell(249, 817, LEFT_TEXT_END, 888),
        cell(RIGHT_TEXT_START, 183, 1414, 540),
        cell(RIGHT_TEXT_START, 611, 1414, 1253),
    ]


def span(cells, top=LEFT_BOX[1], bottom=LEFT_BOX[3]):
    """The band of one page, in pixels rather than fractions.

    :param cells: The page's cells.
    :param top: The top of the column boxes, in render pixels.
    :param bottom: Their bottom, in render pixels.
    :returns: ``(x0, x1)`` in render pixels, or None.
    """
    band = columns.gutter_span(cells, top / HEIGHT, bottom / HEIGHT)
    return None if band is None else (band[0] * WIDTH, band[1] * WIDTH)


class TestGutterSpan(TestCase):
    """The band comes from the cells the two columns cover."""

    def test_the_band_lies_between_the_two_groups(self):
        got = span(cells_of(*body_cells()))

        self.assertIsNotNone(got)
        self.assertAlmostEqual(got[0], LEFT_TEXT_END, places=3)
        self.assertAlmostEqual(got[1], RIGHT_TEXT_START, places=3)

    def test_the_running_head_does_not_bridge_the_gutter(self):
        """One cell holds the whole head, and it spans the gutter. The
        vertical test is what keeps it out of the projection."""
        head = [c for c in body_cells() if c["category"] == "Page-header"][0]

        self.assertLess(head["bbox"][0], LEFT_TEXT_END)
        self.assertGreater(head["bbox"][2], RIGHT_TEXT_START)
        self.assertIsNotNone(span(cells_of(*body_cells())))

    def test_a_full_width_cell_is_dropped(self):
        table = cell(300, 900, 1370, 1100)

        got = span(cells_of(*body_cells(), table))

        self.assertIsNotNone(got)
        self.assertAlmostEqual(got[0], LEFT_TEXT_END, places=3)

    def test_a_cell_that_bridges_the_gutter_hides_it(self):
        """A footnote at 46 % of the page is under WIDE_CELL_RATIO, so
        it is kept, and it leaves the page with no blank band."""
        footnote = cell(246, 1952, 1183, 1983)

        self.assertIsNone(span(cells_of(*body_cells(), footnote)))

    def test_a_band_off_the_middle_is_refused(self):
        edge_pair = [cell(100, 183, 300, 801), cell(350, 183, 1600, 888)]

        self.assertIsNone(span(cells_of(*edge_pair)))

    def test_no_cells_gives_no_band(self):
        self.assertIsNone(columns.gutter_span(None, 0.0, 1.0))


class TestInnerEdges(TestCase):
    """The one rule, in fractions of the page."""

    def edges(self, left, right, band, nudge=columns.NUDGE_PX / WIDTH):
        """Answer one pair, in fractions.

        :param left: The left box's ``(x0, x1)``, in fractions.
        :param right: The right box's ``(x0, x1)``, in fractions.
        :param band: The band, in fractions, or None.
        :param nudge: How far the fallback pulls each edge back, in
            fractions.
        :returns: The answer.
        """
        return columns.inner_edges(left, right, band, nudge)

    def test_a_touching_pair_takes_the_band(self):
        got = self.edges((0.1, 0.5), (0.5, 0.9), (0.48, 0.52))

        self.assertEqual(got, (0.48, 0.52))

    def test_an_edge_never_moves_outwards(self):
        """A pair already clear of the band keeps its edges: a column
        that grows takes in the facing one, which is the fault this
        exists to remove."""
        self.assertIsNone(self.edges((0.1, 0.45), (0.55, 0.9), (0.5, 0.52)))

    def test_a_pair_inside_the_band_is_narrowed_to_it(self):
        """Both edges stand in blank space, so both move to the text."""
        got = self.edges((0.1, 0.48), (0.52, 0.9), (0.44, 0.56))

        self.assertEqual(got, (0.44, 0.56))

    def test_only_the_edge_that_needs_it_moves(self):
        got = self.edges((0.1, 0.5), (0.52, 0.9), (0.48, 0.51))

        self.assertEqual(got, (0.48, 0.52))

    def test_a_band_that_halves_a_column_falls_back_to_the_nudge(self):
        nudge = 0.001

        got = self.edges((0.1, 0.5), (0.5, 0.9), (0.2, 0.52), nudge=nudge)

        self.assertEqual(got, (0.5 - nudge, 0.5 + nudge))

    def test_a_touching_pair_without_a_band_gets_the_gap(self):
        nudge = 0.001

        got = self.edges((0.1, 0.5), (0.5, 0.9), None, nudge=nudge)

        self.assertEqual(got, (0.5 - nudge, 0.5 + nudge))
        self.assertAlmostEqual((got[0] + got[1]) / 2, 0.5, places=9)

    def test_a_separated_pair_without_a_band_is_left_alone(self):
        self.assertIsNone(self.edges((0.1, 0.45), (0.55, 0.9), None))

    def test_a_degenerate_box_is_left_alone(self):
        self.assertIsNone(self.edges((0.5, 0.5), (0.5, 0.9), (0.48, 0.52)))


class TestSeparateRows(TestCase):
    """The rows the viewer draws and the geometry reads."""

    def setUp(self):
        self.scan = ScanFactory()

    def column(self, box, **kwargs):
        """Write one ``TEXT_COLUMN`` row.

        :param box: ``(x0, y0, x1, y1)`` in render pixels.
        :param kwargs: Overrides for the row.
        :returns: The row.
        """
        return Detection.objects.create(
            scan=self.scan,
            page_index=kwargs.pop("page_index", 0),
            label="TEXT_COLUMN",
            label_id=int(Label.TEXT_COLUMN),
            confidence=0.96,
            x0=box[0],
            y0=box[1],
            x1=box[2],
            y1=box[3],
            img_width=WIDTH,
            img_height=HEIGHT,
            **kwargs,
        )

    def test_the_pair_takes_the_cell_edges(self):
        left = self.column(LEFT_BOX)
        right = self.column(RIGHT_BOX)

        written = columns.separate_rows(
            self.scan, {0: cells_of(*body_cells())}
        )

        left.refresh_from_db()
        right.refresh_from_db()
        self.assertEqual(written, 2)
        self.assertAlmostEqual(left.x1, LEFT_TEXT_END, places=1)
        self.assertAlmostEqual(right.x0, RIGHT_TEXT_START, places=1)
        self.assertEqual(left.x0, LEFT_BOX[0], "the outer edge stays")
        self.assertEqual(right.x1, RIGHT_BOX[2], "the outer edge stays")

    def test_a_second_call_writes_nothing(self):
        self.column(LEFT_BOX)
        self.column(RIGHT_BOX)
        cells = {0: cells_of(*body_cells())}
        columns.separate_rows(self.scan, cells)

        self.assertEqual(columns.separate_rows(self.scan, cells), 0)

    def test_without_cells_the_pair_gets_the_fallback_gap(self):
        left = self.column(LEFT_BOX)
        right = self.column(RIGHT_BOX)

        columns.separate_rows(self.scan, {})

        left.refresh_from_db()
        right.refresh_from_db()
        self.assertLess(left.x1, right.x0, "the pair has a gutter")
        self.assertAlmostEqual((left.x1 + right.x0) / 2, LEFT_BOX[2], places=1)

    def test_a_page_of_one_column_is_left_alone(self):
        only = self.column(LEFT_BOX)

        self.assertEqual(columns.separate_rows(self.scan, {}), 0)

        only.refresh_from_db()
        self.assertEqual(only.x1, LEFT_BOX[2])

    def test_a_hand_drawn_pair_is_not_written(self):
        """A box reaches the database exactly as the reviewer drew it.
        ``separate_document`` corrects the geometry's copy instead."""
        left = self.column(
            LEFT_BOX, model_name=Detection.ModelName.MANUAL, found_by=[]
        )
        self.column(RIGHT_BOX)

        self.assertEqual(
            columns.separate_rows(self.scan, {0: cells_of(*body_cells())}), 0
        )

        left.refresh_from_db()
        self.assertEqual(left.x1, LEFT_BOX[2])

    def test_a_withdrawn_row_is_not_read(self):
        self.column(LEFT_BOX)
        self.column(RIGHT_BOX, active=False)

        self.assertEqual(
            columns.separate_rows(self.scan, {0: cells_of(*body_cells())}), 0
        )


class TestSeparateDocument(TestCase):
    """The last word on the boxes the geometry reads."""

    @staticmethod
    def document(*boxes):
        """A one-page document holding ``boxes`` as its columns.

        :param boxes: ``(x0, y0, x1, y1)`` per column, in render pixels.
        :returns: An object with the one attribute the pass reads.
        """
        page = Page(
            index=0,
            pdf_width=612.0,
            pdf_height=792.0,
            img_width=WIDTH,
            img_height=HEIGHT,
        )
        page.detections = [
            BLDetection(
                bbox=BBox(x1=b[0], y1=b[1], x2=b[2], y2=b[3]),
                label=Label.TEXT_COLUMN,
                confidence=0.96,
                page_index=0,
            )
            for b in boxes
        ]
        return SimpleNamespace(pages=[page])

    def test_the_boxes_take_the_cell_edges(self):
        document = self.document(LEFT_BOX, RIGHT_BOX)

        moved = columns.separate_document(
            document, {0: cells_of(*body_cells())}
        )

        boxes = [d.bbox for d in document.pages[0].detections]
        self.assertEqual(moved, 2)
        self.assertAlmostEqual(boxes[0].x2, LEFT_TEXT_END, places=1)
        self.assertAlmostEqual(boxes[1].x1, RIGHT_TEXT_START, places=1)

    def test_the_order_of_the_detections_is_kept(self):
        document = self.document(RIGHT_BOX, LEFT_BOX)

        columns.separate_document(document, {0: cells_of(*body_cells())})

        boxes = [d.bbox for d in document.pages[0].detections]
        self.assertAlmostEqual(boxes[0].x1, RIGHT_TEXT_START, places=1)
        self.assertAlmostEqual(boxes[1].x2, LEFT_TEXT_END, places=1)

    def test_a_document_without_columns_is_left_alone(self):
        self.assertEqual(
            columns.separate_document(SimpleNamespace(pages=[]), {}), 0
        )
