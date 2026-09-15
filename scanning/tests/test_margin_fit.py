"""Tests for the margin text box (issue #323).

``margin_fit`` states where a page's printed text is, so that
blackletter can pull the margin strips in off the blots along the page
edge. Tested here: the one rule and the pages it leaves alone, the
writer over a blackletter document, and the wiring that puts the box on
the document the margin measure reads.

blackletter owns every guard, so what is tested here is the
measurement, plus the two facts this app depends on: a page with no
cells answers exactly as it did before, and a box this module writes
survives to ``compute_margin_rects``.
"""

from unittest import mock

from blackletter.models import BBox, Label, Page
from blackletter.models import Detection as BLDetection
from django.test import TestCase

from scanning import margin_fit
from scanning.text_fit import PageCells

#: The page of every fixture: 1700 x 2200 pixels of a 200 dpi render,
#: which is 612 x 792 pt.
WIDTH, HEIGHT = 1700, 2200
POINTS = (612.0, 792.0)


def cells(*boxes, width=WIDTH, height=HEIGHT, fallback=False):
    """The cells of one page.

    :param boxes: The cell boxes, ``(x0, y0, x1, y1)`` in render pixels.
    :param width: The render width, in pixels.
    :param height: The render height, in pixels.
    :param fallback: Whether the worker re-rendered the page.
    :returns: The page's cells.
    """
    return PageCells(float(width), float(height), tuple(boxes), fallback)


def page(index=0, img_width=WIDTH, img_height=HEIGHT, detections=()):
    """One blackletter page of the fixture volume.

    :param index: The page index.
    :param img_width: The detection render's width, in pixels.
    :param img_height: The detection render's height, in pixels.
    :param detections: The page's detections.
    :returns: The page.
    """
    return Page(
        index=index,
        pdf_width=POINTS[0],
        pdf_height=POINTS[1],
        img_width=img_width,
        img_height=img_height,
        detections=list(detections),
    )


def document(*pages):
    """A stand-in for the blackletter document, which is a list of pages.

    :param pages: The pages.
    :returns: An object carrying them.
    """
    return mock.Mock(pages=list(pages))


class TestTextBox(TestCase):
    """The one rule: the union of a page's cells, in that page's pixels."""

    def test_union_of_the_cells(self):
        """The box spans every cell, and no more."""
        box = margin_fit.text_box(
            cells((230, 176, 820, 1980), (860, 176, 1420, 1900)), WIDTH, HEIGHT
        )
        self.assertEqual(box, (230.0, 176.0, 1420.0, 1980.0))

    def test_one_cell_is_enough(self):
        """A page the reader answered with one region still has a box."""
        box = margin_fit.text_box(cells((100, 200, 1600, 2000)), WIDTH, HEIGHT)
        self.assertEqual(box, (100.0, 200.0, 1600.0, 2000.0))

    def test_a_page_with_no_cells_gets_none(self):
        """A failed page, a filtered page (#242) or an insert's new page."""
        self.assertIsNone(margin_fit.text_box(None, WIDTH, HEIGHT))
        self.assertIsNone(margin_fit.text_box(cells(), WIDTH, HEIGHT))

    def test_the_two_renders_need_not_agree(self):
        """The answer is the fraction of the page, scaled to the page.

        The worker re-renders a page over 4500 px at 72 dpi instead of
        200 (``PageCells.fallback``), so the cells of that page are in
        a render of their own. A fraction of the page is the same
        fraction either way.
        """
        half = cells(
            (425, 550, 1275, 1650), width=3400, height=4400, fallback=True
        )
        box = margin_fit.text_box(half, WIDTH, HEIGHT)
        self.assertEqual(box, (212.5, 275.0, 637.5, 825.0))

    def test_a_render_with_no_size_gets_none(self):
        """Nothing can be scaled without both frames."""
        self.assertIsNone(
            margin_fit.text_box(
                cells((10, 10, 100, 100), width=0), WIDTH, HEIGHT
            )
        )
        self.assertIsNone(
            margin_fit.text_box(cells((10, 10, 100, 100)), 0, HEIGHT)
        )

    def test_a_cell_past_the_render_is_held_inside_it(self):
        """One stray cell must not cost the page its fit.

        ``layout_json.rescale`` clamps nothing, so a cell can arrive
        outside the render. The box is a union, and blackletter refuses
        one past the page's pixels, so the page would fall back to its
        ink box over a single bad cell.
        """
        box = margin_fit.text_box(
            cells((230, 176, 1420, 1980), (1690, 2190, 1900, 2400)),
            WIDTH,
            HEIGHT,
        )
        self.assertEqual(box, (230.0, 176.0, float(WIDTH), float(HEIGHT)))

    def test_a_negative_cell_is_held_at_the_page_edge(self):
        """The same rule on the other side of the render."""
        box = margin_fit.text_box(cells((-40, -10, 1420, 1980)), WIDTH, HEIGHT)
        self.assertEqual(box, (0.0, 0.0, 1420.0, 1980.0))

    def test_an_inverted_box_gets_none(self):
        """A cell with no extent says nothing about the page."""
        self.assertIsNone(
            margin_fit.text_box(cells((100, 100, 100, 100)), WIDTH, HEIGHT)
        )


class TestFitPages(TestCase):
    """The writer over a blackletter document."""

    def test_it_sets_the_box_on_the_page(self):
        """The field the margin measure reads."""
        doc = document(page(index=0))
        written = margin_fit.fit_pages(doc, {0: cells((230, 176, 1420, 1980))})
        self.assertEqual(written, 1)
        self.assertEqual(doc.pages[0].text_box, (230.0, 176.0, 1420.0, 1980.0))

    def test_a_page_with_no_entry_keeps_none(self):
        """One page's missing read costs that page the fit, and no other."""
        doc = document(page(index=0), page(index=1))
        written = margin_fit.fit_pages(doc, {1: cells((230, 176, 1420, 1980))})
        self.assertEqual(written, 1)
        self.assertIsNone(doc.pages[0].text_box)
        self.assertIsNotNone(doc.pages[1].text_box)

    def test_no_cells_at_all_writes_nothing(self):
        """A volume with no OCR document keeps every page as it was."""
        doc = document(page(index=0), page(index=1))
        self.assertEqual(margin_fit.fit_pages(doc, {}), 0)
        self.assertTrue(all(p.text_box is None for p in doc.pages))

    def test_a_page_keeps_its_own_frame(self):
        """Two pages of different render sizes each get their own box."""
        doc = document(
            page(index=0), page(index=1, img_width=850, img_height=1100)
        )
        cell_map = {
            0: cells((425, 550, 1275, 1650)),
            1: cells((425, 550, 1275, 1650)),
        }
        margin_fit.fit_pages(doc, cell_map)
        self.assertEqual(doc.pages[0].text_box, (425.0, 550.0, 1275.0, 1650.0))
        self.assertEqual(doc.pages[1].text_box, (212.5, 275.0, 637.5, 825.0))

    def test_a_document_with_no_pages(self):
        """The empty document of a scan with no detections."""
        self.assertEqual(
            margin_fit.fit_pages(document(), {0: cells((1, 1, 2, 2))}), 0
        )


class TestBlackletterReadsIt(TestCase):
    """What the library does with the box, pinned from this side.

    These are blackletter's rules, not this app's. They are pinned here
    because the whole change is worthless if the field stops being read,
    and because a page with no box must answer exactly as it does today.
    """

    def _bounds(self, text_box, detections=()):
        """The content box blackletter tightens to, for one page.

        :param text_box: The page's text box, or None.
        :param detections: The page's detections.
        :returns: ``(left, top, right, bottom)`` in points.
        """
        from blackletter import margins

        subject = page(detections=detections)
        subject.text_box = text_box
        # The ink of a blotted page: the box runs to the page edge on
        # the right and at the foot.
        measured = (90.0, 33.0, 575.0, 792.0)
        return margins._tighten_bounds(
            measured, subject, None, margins.DEFAULT_BUFFER
        )

    def test_the_box_pulls_the_content_in(self):
        """A blot outside the text no longer holds the strips off it."""
        # The text of the page ends at 1450 px (522 pt) and 1900 px
        # (684 pt); the blot runs past both. Each bound keeps
        # DEFAULT_BUFFER of slack, the same the strips leave round the
        # ink, so the content box ends 5 pt further out.
        bounds = self._bounds((250.0, 100.0, 1450.0, 1900.0))
        self.assertAlmostEqual(bounds[2], 527.0, places=0)
        self.assertAlmostEqual(bounds[3], 689.0, places=0)

    def test_a_page_with_no_box_is_unchanged(self):
        """Today's answer is the floor."""
        self.assertEqual(self._bounds(None), (90.0, 33.0, 575.0, 792.0))

    def test_a_partial_read_is_refused(self):
        """The cells of a page the reader half answered say too little."""
        # A box over the top fifth of the page keeps well under the
        # MARGIN_MIN_KEEP_RATIO floor.
        self.assertEqual(
            self._bounds((250.0, 100.0, 1450.0, 450.0)),
            (90.0, 33.0, 575.0, 792.0),
        )

    def test_the_box_never_cuts_a_column_detection(self):
        """A line the reader missed is still under the model's box."""
        column = BLDetection(
            bbox=BBox(x1=250.0, y1=100.0, x2=1450.0, y2=1990.0),
            label=Label.TEXT_COLUMN,
            confidence=0.95,
            page_index=0,
        )
        bounds = self._bounds(
            (250.0, 100.0, 1450.0, 1900.0), detections=(column,)
        )
        self.assertAlmostEqual(bounds[3], 716.0, places=0)
