"""Unit tests for the per-page sync helpers in ``scanning.services``.

Focus is the sandwich-rule blank-page detector
(``_is_blank_via_sandwich``) and its boundary rect-coverage
confirmation. The helper is a pure function over data structures plus
an optional PDF path; we exercise it directly without touching the
DB.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import fitz
from django.test import SimpleTestCase

from scanning.services import _is_blank_via_sandwich, _page_body_covered

PAGE_W, PAGE_H = 612.0, 792.0
HEADER_PTS = 60.0


def _rect(x0, y0, x1, y1, rtype="headnote"):
    """Build a redaction rect dict in PDF points."""
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "type": rtype}


def _hn(page_index: int) -> dict:
    """Build a stand-in ``headnote`` rect for a given page.

    The sandwich rule only looks at the rect's ``type`` field, so a
    minimal dict with ``type="headnote"`` is enough.
    """
    return {"type": "headnote"}


class IsBlankViaSandwichTests(SimpleTestCase):
    """Tests for ``_is_blank_via_sandwich``."""

    # A long opinion that has interior pages — the only kind the
    # sandwich rule can fire on (``page_count > 2``).
    OPINION = {"caption_page": 3, "key_page": 8, "page_count": 6}

    def test_strict_sandwich_fires_when_both_neighbors_have_headnotes(
        self,
    ):
        """Page is interior of a multi-page opinion AND both neighbor
        pages have headnote rects → blank.

        This is the "fully interior" headnote-block page: the rule
        fires without needing the boundary text-extraction fallback.
        """
        page_index = 5  # interior of opinion: 3 < 5 < 8
        pages = {"4": [_hn(4)], "5": [_hn(5)], "6": [_hn(6)]}
        self.assertTrue(
            _is_blank_via_sandwich(
                page_index=page_index,
                opinions=[self.OPINION],
                redactions_pages=pages,
                page_detections=[],
            )
        )

    def test_neither_neighbor_has_headnote_does_not_fire(self):
        """A page with headnotes but no neighbor headnotes is not the
        interior of a block; the rule does not fire.
        """
        pages = {"5": [_hn(5)]}  # no 4, no 6
        self.assertFalse(
            _is_blank_via_sandwich(
                page_index=5,
                opinions=[self.OPINION],
                redactions_pages=pages,
                page_detections=[],
            )
        )

    def test_last_page_of_block_fires_when_body_is_covered(self):
        """Boundary case — only the previous neighbor has headnotes
        (this is the trailing edge of a headnote block) AND the page's
        rects cover the whole body → blank.

        We mock the coverage check so the test doesn't need a real file
        on disk.
        """
        pages = {"4": [_hn(4)], "5": [_hn(5)]}  # no 6 — block ends here
        with patch(
            "scanning.services._page_body_covered", return_value=True
        ) as mock_covered:
            result = _is_blank_via_sandwich(
                page_index=5,
                opinions=[self.OPINION],
                redactions_pages=pages,
                page_detections=[],
                pdf_path="/fake/page_0006.pdf",
            )
        self.assertTrue(result)
        mock_covered.assert_called_once()

    def test_last_page_of_block_does_not_fire_when_body_uncovered(self):
        """Boundary case — only the previous neighbor has headnotes,
        but part of the body is left unredacted (the headnote block ended
        partway down the page and body picks up below) → not blank.
        """
        pages = {"4": [_hn(4)], "5": [_hn(5)]}
        with patch("scanning.services._page_body_covered", return_value=False):
            result = _is_blank_via_sandwich(
                page_index=5,
                opinions=[self.OPINION],
                redactions_pages=pages,
                page_detections=[],
                pdf_path="/fake/page_0006.pdf",
            )
        self.assertFalse(result)

    def test_first_page_of_block_fires_when_body_is_covered(self):
        """Boundary case — only the NEXT neighbor has headnotes (this
        is the leading edge of a block, just after the caption page).
        Same coverage confirmation as the trailing-edge case.
        """
        pages = {"5": [_hn(5)], "6": [_hn(6)]}  # no 4 — block starts here
        with patch("scanning.services._page_body_covered", return_value=True):
            result = _is_blank_via_sandwich(
                page_index=5,
                opinions=[self.OPINION],
                redactions_pages=pages,
                page_detections=[],
                pdf_path="/fake/page_0006.pdf",
            )
        self.assertTrue(result)

    def test_boundary_case_without_pdf_path_does_not_fire(self):
        """If the boundary case applies but no PDF path was supplied,
        we can't verify body emptiness → fall through to not-blank.

        Conservative default: never flag a page blank without
        evidence.
        """
        pages = {"4": [_hn(4)], "5": [_hn(5)]}  # only prev
        self.assertFalse(
            _is_blank_via_sandwich(
                page_index=5,
                opinions=[self.OPINION],
                redactions_pages=pages,
                page_detections=[],
                pdf_path=None,
            )
        )

    def test_footnotes_detection_overrides_blank(self):
        """A page with a ``FOOTNOTES`` detection is never blank, even
        if both neighbors have headnotes — the footnote band is
        readable content.
        """
        pages = {"4": [_hn(4)], "5": [_hn(5)], "6": [_hn(6)]}
        self.assertFalse(
            _is_blank_via_sandwich(
                page_index=5,
                opinions=[self.OPINION],
                redactions_pages=pages,
                page_detections=[{"label": "FOOTNOTES"}],
            )
        )

    def test_caption_page_does_not_fire(self):
        """The caption page (``page_index == caption_page``) is not
        interior of the opinion's interior pages range, so the rule
        does not fire even if neighbors are sandwiched.
        """
        pages = {"2": [_hn(2)], "3": [_hn(3)], "4": [_hn(4)]}
        self.assertFalse(
            _is_blank_via_sandwich(
                page_index=3,  # == caption_page, not interior
                opinions=[self.OPINION],
                redactions_pages=pages,
                page_detections=[],
            )
        )

    def test_two_page_opinion_never_fires(self):
        """Opinions with ``page_count <= 2`` have no interior pages,
        so the rule never fires regardless of headnotes.
        """
        short = {"caption_page": 3, "key_page": 4, "page_count": 2}
        pages = {"2": [_hn(2)], "3": [_hn(3)], "4": [_hn(4)]}
        self.assertFalse(
            _is_blank_via_sandwich(
                page_index=3,
                opinions=[short],
                redactions_pages=pages,
                page_detections=[],
            )
        )


class PageBodyCoveredTests(SimpleTestCase):
    """Tests for ``_page_body_covered``.

    Replaces the old text-extraction probe: the generated per-page PDFs
    no longer carry a text layer, so body emptiness is measured from the
    redaction geometry instead.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.pdf_path = Path(self._tmp.name) / "page_0006.pdf"
        with fitz.open() as doc:
            doc.new_page(width=PAGE_W, height=PAGE_H)
            doc.save(str(self.pdf_path))

    def test_fully_covered_body_is_blank(self):
        pages = {"5": [_rect(0, HEADER_PTS, PAGE_W, PAGE_H)]}
        self.assertTrue(_page_body_covered(pages, 5, self.pdf_path))

    def test_header_band_does_not_need_covering(self):
        """The printed page number lives above the body and is kept."""
        pages = {"5": [_rect(0, HEADER_PTS, PAGE_W, PAGE_H)]}
        self.assertTrue(_page_body_covered(pages, 5, self.pdf_path))
        # A rect that starts below the header leaves body uncovered.
        pages = {"5": [_rect(0, 400, PAGE_W, PAGE_H)]}
        self.assertFalse(_page_body_covered(pages, 5, self.pdf_path))

    def test_margins_plus_headnotes_count_together(self):
        """Headnote rects never reach the page edges; margins do."""
        pages = {
            "5": [
                _rect(0, 0, 72, PAGE_H, "margin"),
                _rect(540, 0, PAGE_W, PAGE_H, "margin"),
                _rect(72, HEADER_PTS, 540, PAGE_H),
            ]
        }
        self.assertTrue(_page_body_covered(pages, 5, self.pdf_path))

    def test_partly_covered_body_is_not_blank(self):
        """A headnote block ending mid-page leaves live text below."""
        pages = {"5": [_rect(72, HEADER_PTS, 540, 400)]}
        self.assertFalse(_page_body_covered(pages, 5, self.pdf_path))

    def test_no_rects_is_not_blank(self):
        self.assertFalse(_page_body_covered({}, 5, self.pdf_path))

    def test_unreadable_pdf_is_not_blank(self):
        """Never flag a page blank when the check itself failed."""
        pages = {"5": [_rect(0, HEADER_PTS, PAGE_W, PAGE_H)]}
        missing = Path(self._tmp.name) / "does_not_exist.pdf"
        self.assertFalse(_page_body_covered(pages, 5, missing))
