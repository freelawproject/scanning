"""Unit tests for ``ai.user_prompt``.

These tests cover the pure-Python paths that don't require a real
PDF on disk: geometry classifiers, the blank-page short-circuit,
and the pass-through-from-opinions-data path through
``_opinion_roadmap``. Tests that exercise the caption / footnote
text-crop paths need a real per-page PDF and a Page row, and live
alongside the scanning integration tests.
"""

from __future__ import annotations

from types import SimpleNamespace

from django.test import SimpleTestCase

from ai.user_prompt import (
    _BLANK_PAGE_INSTRUCTION,
    _bbox_col,
    _bbox_zone,
    _opinion_roadmap,
    build_user_prompt,
)


class BBoxClassifierTests(SimpleTestCase):
    """Tests for the geometry helpers that classify bbox positions."""

    def test_bbox_col_left(self):
        """A bbox whose center is in the left half resolves to ``L``."""
        # center x = 400 (canvas is 1700 wide, mid = 850)
        self.assertEqual(_bbox_col([200, 0, 600, 100]), "L")

    def test_bbox_col_right(self):
        """A bbox whose center is in the right half resolves to ``R``."""
        # center x = 1300
        self.assertEqual(_bbox_col([1000, 0, 1600, 100]), "R")

    def test_bbox_col_at_midpoint_resolves_right(self):
        """A bbox centered exactly at the midpoint resolves to ``R``.

        The split predicate is ``< mid``, so the midpoint itself is
        the right column.
        """
        # center x = 850
        self.assertEqual(_bbox_col([800, 0, 900, 100]), "R")

    def test_bbox_zone_top(self):
        """A bbox in the top third resolves to ``top``."""
        # canvas is 2200 tall; first third ends at y ≈ 733
        # center y = 300
        self.assertEqual(_bbox_zone([0, 200, 100, 400]), "top")

    def test_bbox_zone_middle(self):
        """A bbox in the middle third resolves to ``middle``."""
        # center y = 1100
        self.assertEqual(_bbox_zone([0, 1000, 100, 1200]), "middle")

    def test_bbox_zone_bottom(self):
        """A bbox in the bottom third resolves to ``bottom``."""
        # center y = 1900
        self.assertEqual(_bbox_zone([0, 1800, 100, 2000]), "bottom")


class BuildUserPromptTests(SimpleTestCase):
    """Tests for the ``build_user_prompt`` public entry."""

    def _make_page(self, **overrides):
        """Build a duck-typed Page stand-in for unit tests.

        The real ``Page`` is a Django model that needs DB access; for
        these tests we only need an object with the few attributes
        the builder reads.
        """
        defaults = dict(
            is_blank=False,
            detections=[],
            page_index=0,
            pdf_path="llm/page_0001.pdf",
            scan=SimpleNamespace(output_dir="/tmp/x", opinions_json=[]),
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_blank_page_short_circuits_to_canned_instruction(self):
        """A page flagged ``is_blank=True`` returns the canned BLANK
        instruction without touching the PDF.

        This is the auto-blank short-circuit: when the sandwich rule
        flags a page as entirely-covered-by-headnote at sync time,
        we never open the PDF for it.
        """
        page = self._make_page(is_blank=True)
        self.assertEqual(build_user_prompt(page), _BLANK_PAGE_INSTRUCTION)


class OpinionRoadmapTests(SimpleTestCase):
    """Tests for the ``_opinion_roadmap`` section builder."""

    def test_empty_inputs_return_none(self):
        """No captions + no keys + no opinions touching this page →
        the builder has nothing to say, returns ``None``.
        """
        result = _opinion_roadmap(
            detections=[], opinions=[], page_index=5, pdf_page=None
        )
        self.assertIsNone(result)

    def test_pure_pass_through_from_opinions_data(self):
        """No detections + an opinion that spans this page emits the
        'passes through' prompt.

        Pass-through is the one fact the page-local detections can't
        supply, so we read it from ``scan.opinions_json``. The
        ``pdf_page`` argument is never touched on this path.
        """
        opinions = [{"caption_page": 0, "key_page": 10}]
        result = _opinion_roadmap(
            detections=[], opinions=opinions, page_index=5, pdf_page=None
        )
        self.assertIsNotNone(result)
        self.assertIn("1 opinion(s) visible", result)
        self.assertIn("passes through this page", result)

    def test_pass_through_count_aggregates_across_opinions(self):
        """Multiple opinions spanning the same page bump the count."""
        opinions = [
            {"caption_page": 0, "key_page": 10},
            {"caption_page": 2, "key_page": 8},
        ]
        result = _opinion_roadmap(
            detections=[], opinions=opinions, page_index=5, pdf_page=None
        )
        self.assertIn("2 opinion(s) visible", result)
        self.assertIn("2 passes through this page", result)

    def test_opinion_ending_exactly_here_is_not_pass_through(self):
        """An opinion whose ``key_page`` equals this page index is
        ending here, not passing through.

        Pass-through requires ``caption_page < page_index < key_page``
        strictly. With no detections on the page we have no signal
        for the ends-here case, so the roadmap returns ``None``.
        """
        opinions = [{"caption_page": 0, "key_page": 5}]
        result = _opinion_roadmap(
            detections=[], opinions=opinions, page_index=5, pdf_page=None
        )
        self.assertIsNone(result)
