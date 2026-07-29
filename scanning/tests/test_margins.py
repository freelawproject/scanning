"""Tests for ``scanning.margins``.

The pipeline no longer embeds a text layer, so margin rects have to be
measurable from a text-less ``bitonal.pdf``. These tests build small
synthetic PDFs (a text page, the same page rasterized to 1-bit like
``bitonal.pdf``, and one with a scanner edge bar) and check the content
box the margin rects leave uncovered against the page's actual ink,
measured independently with a plain dark-pixel scan.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import fitz
import numpy as np
from django.test import SimpleTestCase

from scanning.margins import compute_margin_rects
from scanning.tests.pdf_fixtures import (
    BLEED_MARK,
    BOTTOM_BAR,
    CONTENT,
    CORNER_NUMBER_X,
    HEADER_LINE_Y,
    PAGE_H,
    PAGE_W,
    STRAY_MARK,
    TOP_BAR,
    detection,
    rasterize,
    write_bitonal_page,
    write_text_page,
)


def _ink_bbox(pdf_path: Path) -> tuple[float, float, float, float]:
    """Measure where the marks are on page 0, in PDF points.

    Deliberately naive (any dark pixel counts) so it is independent of
    the heuristics under test. Used as the reference box.

    :param pdf_path: PDF to measure.
    :return: ``(left, top, right, bottom)`` in PDF points.
    """
    with fitz.open(str(pdf_path)) as doc:
        page = doc[0]
        pix = page.get_pixmap(dpi=200, colorspace=fitz.csGRAY)
        gray = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.stride
        )[:, : pix.width]
        dark = gray < 200
        rows = np.flatnonzero(dark.any(axis=1))
        cols = np.flatnonzero(dark.any(axis=0))
        sx = page.rect.width / pix.width
        sy = page.rect.height / pix.height
    return (
        cols[0] * sx,
        rows[0] * sy,
        (cols[-1] + 1) * sx,
        (rows[-1] + 1) * sy,
    )


def _uncovered_box(rects: list[dict]) -> tuple[float, float, float, float]:
    """Derive the region a page's margin rects leave uncovered.

    Works off a coverage grid rather than the rects' shapes, so it does not
    care how the strips are cut up.

    :param rects: The ``rects`` list for one page.
    :return: ``(left, top, right, bottom)`` in PDF points.
    """
    cell = 1.0
    cols, rows = int(PAGE_W / cell), int(PAGE_H / cell)
    covered = np.zeros((rows, cols), dtype=bool)
    for r in rects:
        covered[
            max(0, int(r["y0"] / cell)) : int(r["y1"] / cell),
            max(0, int(r["x0"] / cell)) : int(r["x1"] / cell),
        ] = True
    free_rows = np.flatnonzero(~covered.all(axis=1))
    free_cols = np.flatnonzero(~covered.all(axis=0))
    if not free_rows.size or not free_cols.size:
        return (0.0, 0.0, 0.0, 0.0)
    return (
        free_cols[0] * cell,
        free_rows[0] * cell,
        (free_cols[-1] + 1) * cell,
        (free_rows[-1] + 1) * cell,
    )


def _rects_for(result: list[dict], page_index: int = 0) -> list[dict]:
    """Pull one page's rects out of a compute_margin_rects result."""
    return next(e for e in result if e["page_index"] == page_index)["rects"]


class ComputeMarginRectsTests(SimpleTestCase):
    """Content box detection with and without a text layer."""

    # The measured box may sit a few points outside the ink (the safety
    # buffer, glyph bounds, ink rounding at 100 dpi) but must never cut
    # into it.
    SLACK = 16.0

    def assert_box_matches_ink(self, rects, ink_box):
        """Assert the uncovered box contains the ink and stays tight."""
        left, top, right, bottom = _uncovered_box(rects)
        ink_left, ink_top, ink_right, ink_bottom = ink_box
        self.assertLessEqual(left, ink_left, "left margin covers content")
        self.assertLessEqual(top, ink_top, "top margin covers content")
        self.assertGreaterEqual(
            right, ink_right, "right margin covers content"
        )
        self.assertGreaterEqual(
            bottom, ink_bottom, "bottom margin covers content"
        )
        self.assertGreater(left, ink_left - self.SLACK)
        self.assertGreater(top, ink_top - self.SLACK)
        self.assertLess(right, ink_right + self.SLACK)
        self.assertLess(bottom, ink_bottom + self.SLACK)

    def test_text_layer_page(self):
        """A page with text is measured from its text blocks."""
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "text.pdf"
            write_text_page(pdf)
            result = compute_margin_rects(pdf)
            ink_box = _ink_bbox(pdf)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["page_index"], 0)
        self.assertTrue(_rects_for(result), "no margin rects computed")
        self.assert_box_matches_ink(_rects_for(result), ink_box)

    def test_textless_page_measured_from_ink(self):
        """A bitonal page has no text, so ink defines the content box."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "text.pdf"
            pdf = Path(tmp) / "bitonal.pdf"
            write_text_page(src)
            rasterize(src, pdf)
            with fitz.open(str(pdf)) as doc:
                self.assertEqual(doc[0].get_text("text").strip(), "")
            result = compute_margin_rects(pdf)
            ink_box = _ink_bbox(pdf)

        self.assertTrue(_rects_for(result), "no margin rects computed")
        self.assert_box_matches_ink(_rects_for(result), ink_box)

    def test_scanner_edge_bar_is_excluded(self):
        """A solid bar at the page edge must not become content."""
        with tempfile.TemporaryDirectory() as tmp:
            clean_src = Path(tmp) / "clean.pdf"
            bar_src = Path(tmp) / "bar.pdf"
            pdf = Path(tmp) / "bitonal.pdf"
            write_text_page(clean_src)
            write_text_page(bar_src, top_bar=True)
            rasterize(bar_src, pdf)
            result = compute_margin_rects(pdf)
            # Reference is the page without the artifact: the bar must
            # not move the content box.
            ink_box = _ink_bbox(clean_src)

        rects = _rects_for(result)
        self.assertTrue(rects, "no margin rects computed")
        _left, top, _right, _bottom = _uncovered_box(rects)
        # The bar sits at y=4..12, so the top margin rect must reach past
        # it for cleanup to cover it.
        self.assertGreater(top, 12)
        self.assert_box_matches_ink(rects, ink_box)

    def test_blank_page_gets_no_rects(self):
        """Nothing to measure means leave the page alone."""
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "blank.pdf"
            with fitz.open() as doc:
                doc.new_page(width=PAGE_W, height=PAGE_H)
                doc.save(str(pdf))
            result = compute_margin_rects(pdf)

        self.assertEqual(_rects_for(result), [])
        self.assertEqual(result[0]["page_width"], PAGE_W)
        self.assertEqual(result[0]["page_height"], PAGE_H)

    def test_narrow_content_is_skipped(self):
        """Narrow content (image page, appendix) is not a margin boundary."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "narrow.pdf"
            with fitz.open() as doc:
                page = doc.new_page(width=PAGE_W, height=PAGE_H)
                page.insert_text((280, 400), "12", fontsize=9)
                doc.save(str(src))
            # Both paths must skip it: with a text layer and without.
            self.assertEqual(_rects_for(compute_margin_rects(src)), [])
            raster = Path(tmp) / "bitonal.pdf"
            rasterize(src, raster)
            self.assertEqual(_rects_for(compute_margin_rects(raster)), [])


class DetectionTightenedMarginTests(SimpleTestCase):
    """Margins tightened with detection geometry.

    Ink is the union of every mark on a page, so one speck out in a margin
    pushes the content box to it and the strip on that side shrinks away.
    ``TEXT_COLUMN`` boxes bound the printed text instead, and the header row
    bounds it vertically, so the two estimates get intersected.
    """

    BUFFER = 5.0

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _columns(self):
        """TEXT_COLUMN detections spanning the body text."""
        mid = (CONTENT.x0 + CONTENT.x1) / 2
        return [
            detection(
                "TEXT_COLUMN", CONTENT.x0, CONTENT.y0, mid - 6, CONTENT.y1
            ),
            detection(
                "TEXT_COLUMN", mid + 6, CONTENT.y0, CONTENT.x1, CONTENT.y1
            ),
        ]

    def _header(self):
        """The header-row detection a real page carries."""
        return detection(
            "PAGE_HEADER",
            CONTENT.x0,
            HEADER_LINE_Y - 8,
            CONTENT.x1,
            HEADER_LINE_Y + 2,
        )

    def test_column_band_tightens_the_side_strips(self):
        """A speck in the margin no longer decides where a strip stops."""
        pdf = self.tmp / "stray.pdf"
        write_bitonal_page(pdf, stray_mark=True)

        loose = _uncovered_box(_rects_for(compute_margin_rects(pdf)))
        self.assertLessEqual(
            loose[0], STRAY_MARK.x0, "speck should widen the content box"
        )

        tight = _uncovered_box(
            _rects_for(compute_margin_rects(pdf, detections=self._columns()))
        )
        self.assertAlmostEqual(tight[0], CONTENT.x0 - self.BUFFER, delta=2.0)
        self.assertGreater(tight[0], STRAY_MARK.x1, "speck left unmasked")

    def test_header_detection_restores_the_top_strip(self):
        """Bleed-through at the very top edge must not suppress the strip."""
        pdf = self.tmp / "bleed.pdf"
        write_bitonal_page(pdf, header_line=True, bleed_mark=True)

        without = _rects_for(compute_margin_rects(pdf))
        self.assertFalse(
            [r for r in without if r["y0"] <= 1 and r["x1"] - r["x0"] > 400],
            "expected the bleed mark to suppress the top strip",
        )

        dets = self._columns() + [
            detection(
                "PAGE_HEADER",
                CONTENT.x0,
                HEADER_LINE_Y - 8,
                CONTENT.x1,
                HEADER_LINE_Y + 2,
            ),
            # ...and the bleed itself, which YOLO also labels; being inside
            # the top edge band, it must not define the top bound.
            detection(
                "PAGE_NUMBER",
                BLEED_MARK.x0,
                BLEED_MARK.y0,
                BLEED_MARK.x1,
                BLEED_MARK.y1,
            ),
        ]
        rects = _rects_for(compute_margin_rects(pdf, detections=dets))
        top = [r for r in rects if r["y0"] <= 1 and r["x1"] - r["x0"] > 400]
        self.assertTrue(top, "no top strip")
        self.assertGreater(top[0]["y1"], BLEED_MARK.y1, "bleed left unmasked")
        self.assertLess(
            top[0]["y1"], HEADER_LINE_Y - 8, "top strip hits header"
        )

    def test_page_number_outside_the_band_survives(self):
        """A page number printed outside the columns keeps its whitespace.

        The band spans the header row as well as the text columns, so a
        strip tightened to the columns cannot reach a number in the corner.
        """
        pdf = self.tmp / "corner.pdf"
        write_bitonal_page(pdf, header_line=True, corner_number=True)
        page_no = detection(
            "PAGE_NUMBER",
            CORNER_NUMBER_X - 2,
            HEADER_LINE_Y - 9,
            CORNER_NUMBER_X + 12,
            HEADER_LINE_Y + 2,
        )
        rects = _rects_for(
            compute_margin_rects(pdf, detections=self._columns() + [page_no])
        )
        px0, py0, px1, py1 = page_no["bbox"]
        for r in rects:
            overlap_x = min(px1, r["x1"]) - max(px0, r["x0"])
            overlap_y = min(py1, r["y1"]) - max(py0, r["y0"])
            self.assertFalse(
                overlap_x > 1 and overlap_y > 1,
                f"page number covered by margin {r}",
            )
        # ...and a strip is still produced on that side, it just stops
        # short of the number instead of running into it.
        self.assertTrue([r for r in rects if r["x0"] <= 1 and r["x1"] > 1])

    def test_degenerate_band_is_ignored(self):
        """A bogus TEXT_COLUMN box cannot collapse the content box."""
        pdf = self.tmp / "narrow_band.pdf"
        write_bitonal_page(pdf)
        bogus = [detection("TEXT_COLUMN", 300, 300, 320, 320)]
        self.assertEqual(
            _rects_for(compute_margin_rects(pdf, detections=bogus)),
            _rects_for(compute_margin_rects(pdf)),
        )

    def test_margins_never_cover_the_text(self):
        """The acceptance property, with every signal in play."""
        pdf = self.tmp / "all.pdf"
        write_bitonal_page(
            pdf,
            header_line=True,
            bleed_mark=True,
            stray_mark=True,
            top_bar=True,
            bottom_bar=True,
        )
        rects = _rects_for(
            compute_margin_rects(
                pdf, detections=self._columns() + [self._header()]
            )
        )
        left, top, right, bottom = _uncovered_box(rects)
        self.assertLessEqual(left, CONTENT.x0)
        self.assertLessEqual(top, CONTENT.y0)
        self.assertGreaterEqual(right, CONTENT.x1)
        self.assertGreaterEqual(bottom, CONTENT.y1)
        # ...while the edge artifacts are all masked.
        self.assertGreater(left, STRAY_MARK.x1)
        self.assertGreater(top, max(TOP_BAR.y1, BLEED_MARK.y1))
        self.assertLess(bottom, BOTTOM_BAR.y0)
