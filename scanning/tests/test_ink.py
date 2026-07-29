"""Tests for ``scanning.ink``.

These measurements stand in for the PDF text layer the pipeline stopped
producing, so the cases that matter are the ones where ink and text
disagree: scanner artifacts along the page edges, which carry ink but are
not printed text.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import fitz
from django.test import SimpleTestCase

from scanning.ink import (
    content_box,
    grow_to_ink,
    has_text_layer,
    ink_bbox,
    page_mask,
)
from scanning.tests.pdf_fixtures import (
    BOTTOM_BAR,
    COLUMN_LEFT,
    COLUMN_RIGHT,
    CONTENT,
    HEADER_LINE_Y,
    PAGE_H,
    PAGE_W,
    write_bitonal_page,
    write_text_page,
    write_two_column_page,
)

# The measured box lands within a few points of CONTENT: glyphs start
# below the box top, and the last line ends above its bottom.
SLACK = 6.0


class ContentBoxTests(SimpleTestCase):
    """Tests for ``content_box``."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _box(self, pdf_path):
        with fitz.open(str(pdf_path)) as doc:
            return content_box(doc[0])

    def test_finds_the_text_block(self):
        pdf = self.tmp / "clean.pdf"
        write_bitonal_page(pdf)
        left, top, right, bottom = self._box(pdf)
        self.assertAlmostEqual(left, CONTENT.x0, delta=SLACK)
        self.assertAlmostEqual(top, CONTENT.y0, delta=SLACK)
        self.assertAlmostEqual(right, CONTENT.x1, delta=SLACK)
        self.assertAlmostEqual(bottom, CONTENT.y1, delta=SLACK)

    def test_excludes_edge_bars(self):
        """Platen bands at the page edges are not printed content."""
        pdf = self.tmp / "bars.pdf"
        write_bitonal_page(pdf, top_bar=True, bottom_bar=True)
        _left, top, _right, bottom = self._box(pdf)
        self.assertGreater(top, 12, "top bar counted as content")
        self.assertLess(bottom, BOTTOM_BAR.y0, "bottom bar counted as content")

    def test_none_for_blank_page(self):
        pdf = self.tmp / "blank.pdf"
        with fitz.open() as doc:
            doc.new_page(width=PAGE_W, height=PAGE_H)
            doc.save(str(pdf))
        self.assertIsNone(self._box(pdf))

    def test_none_for_narrow_content(self):
        pdf = self.tmp / "narrow.pdf"
        with fitz.open() as doc:
            page = doc.new_page(width=PAGE_W, height=PAGE_H)
            page.insert_text((280, 400), "12", fontsize=9)
            doc.save(str(pdf))
        self.assertIsNone(self._box(pdf))

    def test_cached_per_page(self):
        pdf = self.tmp / "clean.pdf"
        write_bitonal_page(pdf)
        with fitz.open(str(pdf)) as doc:
            first = content_box(doc[0])
            with_cache = content_box(doc[0])
        self.assertIs(first, with_cache)


class InkBboxTests(SimpleTestCase):
    """Tests for ``ink_bbox``."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_measures_the_text_in_a_clip(self):
        pdf = self.tmp / "clean.pdf"
        write_bitonal_page(pdf)
        with fitz.open(str(pdf)) as doc:
            clip = fitz.Rect(0, 0, PAGE_W, PAGE_H)
            box = ink_bbox(doc[0], clip)
        self.assertIsNotNone(box)
        left, top, right, bottom = box
        self.assertAlmostEqual(left, CONTENT.x0, delta=SLACK)
        self.assertAlmostEqual(top, CONTENT.y0, delta=SLACK)
        self.assertAlmostEqual(right, CONTENT.x1, delta=SLACK)
        self.assertAlmostEqual(bottom, CONTENT.y1, delta=SLACK)

    def test_ignores_bottom_edge_artifact(self):
        """The regression that emptied the headnote rects.

        A headnote block's rect runs to the bottom of its column. Measured
        naively, a platen band along the page edge becomes the "last line"
        and the rect stretches to the page edge; clipping to the content
        box keeps it at the real last line.
        """
        pdf = self.tmp / "bottom_bar.pdf"
        write_bitonal_page(pdf, bottom_bar=True)
        with fitz.open(str(pdf)) as doc:
            clip = fitz.Rect(72, 400, 540, PAGE_H)
            box = ink_bbox(doc[0], clip)
        self.assertIsNotNone(box)
        self.assertLess(box[3], BOTTOM_BAR.y0, "measured to the edge band")
        self.assertAlmostEqual(box[3], CONTENT.y1, delta=SLACK)

    def test_none_for_empty_region(self):
        pdf = self.tmp / "clean.pdf"
        write_bitonal_page(pdf)
        with fitz.open(str(pdf)) as doc:
            # Between the content box top and the page edge: no ink.
            box = ink_bbox(doc[0], fitz.Rect(0, 0, PAGE_W, 40))
        self.assertIsNone(box)

    def test_reads_a_page_once(self):
        pdf = self.tmp / "clean.pdf"
        write_bitonal_page(pdf)
        with fitz.open(str(pdf)) as doc:
            page = doc[0]
            mask, _sx, _sy = page_mask(page)
            ink_bbox(page, fitz.Rect(72, 100, 540, 400))
            again, _sx, _sy = page_mask(page)
        self.assertIs(mask, again)


class HasTextLayerTests(SimpleTestCase):
    """Tests for ``has_text_layer``."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_true_for_text_pdf(self):
        pdf = self.tmp / "text.pdf"
        write_text_page(pdf)
        self.assertTrue(has_text_layer(pdf))

    def test_false_for_bitonal_pdf(self):
        pdf = self.tmp / "bitonal.pdf"
        write_bitonal_page(pdf)
        self.assertFalse(has_text_layer(pdf))

    def test_false_for_unreadable_path(self):
        self.assertFalse(has_text_layer(self.tmp / "nope.pdf"))


class GrowToInkTests(SimpleTestCase):
    """Tests for ``grow_to_ink``.

    Redaction rects come from detection geometry, which can sit a few
    points inside the printed text and clip the first character of every
    line or the tail of a line. The text-layer code path absorbed that,
    because word boxes overlapping a rect pulled its bounds outward; ink
    measured inside a rect has to grow back out explicitly.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.pdf = self.tmp / "bitonal.pdf"
        write_bitonal_page(self.pdf, header_line=True, bottom_bar=True)

    def _grow(self, rect):
        with fitz.open(str(self.pdf)) as doc:
            return grow_to_ink(doc[0], rect)

    def test_grows_over_a_clipped_first_character(self):
        """A rect starting inside the text column reaches back out to it."""
        clipped = fitz.Rect(CONTENT.x0 + 10, 300, CONTENT.x1, 400)
        grown = self._grow(clipped)
        self.assertAlmostEqual(grown.x0, CONTENT.x0, delta=4.0)
        self.assertLessEqual(grown.x1, CONTENT.x1 + 4.0)

    def test_grows_over_a_clipped_line(self):
        """A rect whose bottom cuts a line takes in the rest of it."""
        # 300 pt is mid-block, so the edge lands inside a line of text.
        cut = fitz.Rect(CONTENT.x0, 200, CONTENT.x1, 300)
        grown = self._grow(cut)
        self.assertGreater(grown.y1, 300)
        self.assertLess(grown.y1, 300 + 12, "grew past the cut line")

    def test_stops_at_white_space(self):
        """Growth does not jump the blank gap up to the running head."""
        block = fitz.Rect(CONTENT.x0, CONTENT.y0 + 6, CONTENT.x1, 400)
        grown = self._grow(block)
        self.assertGreater(grown.y0, HEADER_LINE_Y + 4, "swallowed the header")

    def test_stays_off_the_platen_band(self):
        """A rect at the foot of the page does not grow onto the edge band."""
        tail = fitz.Rect(CONTENT.x0, 600, CONTENT.x1, CONTENT.y1)
        grown = self._grow(tail)
        self.assertLess(grown.y1, BOTTOM_BAR.y0)

    def test_never_shrinks(self):
        """Growth only ever adds coverage."""
        rect = fitz.Rect(CONTENT.x0 + 20, 250, CONTENT.x1 - 20, 350)
        grown = self._grow(rect)
        self.assertLessEqual(grown.x0, rect.x0)
        self.assertLessEqual(grown.y0, rect.y0)
        self.assertGreaterEqual(grown.x1, rect.x1)
        self.assertGreaterEqual(grown.y1, rect.y1)

    def test_unmeasurable_rect_is_returned_unchanged(self):
        outside = fitz.Rect(0, PAGE_H - 20, 30, PAGE_H)
        self.assertEqual(tuple(self._grow(outside)), tuple(outside))


class GrowAcrossGutterTests(SimpleTestCase):
    """Growth must not cross a hairline gutter into the next column.

    Regression: the outward walk started one pixel column *past* the rect
    edge, stepping over the single blank column that separates tightly set
    columns. A rect then grew across the gutter and swallowed its
    neighbour's text, which on a real page meant a ``TEXT_COLUMN`` box
    widening by 20 pt into the other column.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.pdf = Path(self._tmp.name) / "two_col.pdf"
        write_two_column_page(self.pdf)

    def test_right_column_does_not_grow_into_the_left(self):
        with fitz.open(str(self.pdf)) as doc:
            grown = grow_to_ink(doc[0], fitz.Rect(COLUMN_RIGHT), margin_y=0.0)
        self.assertGreaterEqual(
            grown.x0,
            COLUMN_LEFT.x1 - 1,
            f"grew back over the gutter to {grown.x0}",
        )

    def test_left_column_does_not_grow_into_the_right(self):
        with fitz.open(str(self.pdf)) as doc:
            grown = grow_to_ink(doc[0], fitz.Rect(COLUMN_LEFT), margin_y=0.0)
        self.assertLessEqual(
            grown.x1,
            COLUMN_RIGHT.x0 + 1,
            f"grew forward over the gutter to {grown.x1}",
        )

    def test_a_narrow_box_still_reaches_its_own_text(self):
        """The fix must not stop legitimate growth."""
        inset = fitz.Rect(COLUMN_RIGHT.x0 + 6, 200, COLUMN_RIGHT.x1 - 6, 400)
        with fitz.open(str(self.pdf)) as doc:
            grown = grow_to_ink(doc[0], inset, margin_y=0.0)
        self.assertAlmostEqual(grown.x0, COLUMN_RIGHT.x0, delta=2.0)
        self.assertAlmostEqual(grown.x1, COLUMN_RIGHT.x1, delta=2.0)
