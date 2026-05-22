"""Unit tests for the per-page sync helpers in ``scanning.services``.

Focus is the sandwich-rule blank-page detector
(``_is_blank_via_sandwich``) and its boundary text-extraction
fallback. The helper is a pure function over data structures plus
an optional PDF path; we exercise it directly without touching the
DB.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase

from scanning.services import _is_blank_via_sandwich


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

    def test_last_page_of_block_fires_when_pdf_body_is_textless(self):
        """Boundary case — only the previous neighbor has headnotes
        (this is the trailing edge of a headnote block) AND the
        rendered PDF's body extracts to nothing → blank.

        The per-page PDFs are post-redaction, so an actually-empty
        body extracts to no text. We mock the PDF check so the test
        doesn't need a real file on disk.
        """
        pages = {"4": [_hn(4)], "5": [_hn(5)]}  # no 6 — block ends here
        with patch(
            "scanning.services._page_body_textless", return_value=True
        ) as mock_textless:
            result = _is_blank_via_sandwich(
                page_index=5,
                opinions=[self.OPINION],
                redactions_pages=pages,
                page_detections=[],
                pdf_path="/fake/page_0006.pdf",
            )
        self.assertTrue(result)
        mock_textless.assert_called_once()

    def test_last_page_of_block_does_not_fire_when_body_has_text(self):
        """Boundary case — only the previous neighbor has headnotes,
        but the rendered PDF still has body text (headnote block ended
        partway down the page and body picks up below) → not blank.
        """
        pages = {"4": [_hn(4)], "5": [_hn(5)]}
        with patch(
            "scanning.services._page_body_textless", return_value=False
        ):
            result = _is_blank_via_sandwich(
                page_index=5,
                opinions=[self.OPINION],
                redactions_pages=pages,
                page_detections=[],
                pdf_path="/fake/page_0006.pdf",
            )
        self.assertFalse(result)

    def test_first_page_of_block_fires_when_pdf_body_is_textless(self):
        """Boundary case — only the NEXT neighbor has headnotes (this
        is the leading edge of a block, just after the caption page).
        Same text-extraction confirmation as the trailing-edge case.
        """
        pages = {"5": [_hn(5)], "6": [_hn(6)]}  # no 4 — block starts here
        with patch("scanning.services._page_body_textless", return_value=True):
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
