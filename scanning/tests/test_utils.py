"""Tests for scanning.utils helpers."""

from django.test import TestCase

from scanning.utils import compute_coverage_gaps


def _op(caption_page, key_page, **extra):
    """Build a minimal opinion dict for coverage-gap tests.

    :param caption_page: 0-based pdf index where the opinion starts.
    :type caption_page: int
    :param key_page: 0-based pdf index of the opinion's key icon (end).
    :type key_page: int
    :param extra: Additional opinion keys (e.g. page_end, page_count).
    :returns: An opinion dict.
    :rtype: dict
    """
    return {"caption_page": caption_page, "key_page": key_page, **extra}


class TestComputeCoverageGaps(TestCase):
    def test_no_gaps_when_fully_covered(self):
        opinions = [_op(0, 3)]
        self.assertEqual(compute_coverage_gaps(opinions, 1, 4), [])

    def test_orphan_page_between_opinions_is_a_gap(self):
        # Regression for #111: an opinion's covered span must end at its
        # key_page, not key_page + page_count. A large page_count must not
        # inflate coverage and swallow the orphaned page between opinions.
        opinions = [
            _op(0, 10, page_count=11),  # covers pages 1-11
            _op(12, 22, page_count=11),  # covers pages 13-23
        ]
        # pdf index 11 (page 12) is covered by neither opinion.
        self.assertEqual(compute_coverage_gaps(opinions, 1, 23), [(12, 12, 1)])

    def test_page_count_does_not_extend_coverage(self):
        # A single opinion with an inflated page_count covers only
        # caption_page..key_page; trailing pages remain uncovered.
        opinions = [_op(0, 5, page_count=6)]
        self.assertEqual(compute_coverage_gaps(opinions, 1, 9), [(7, 9, 3)])

    def test_page_end_extends_coverage_beyond_key_page(self):
        # When page_end is present it defines the opinion's last page.
        opinions = [_op(0, 5, page_end=8)]
        self.assertEqual(compute_coverage_gaps(opinions, 1, 9), [])

    def test_gap_before_first_opinion(self):
        opinions = [_op(2, 9)]
        self.assertEqual(compute_coverage_gaps(opinions, 1, 10), [(1, 2, 2)])

    def test_gap_after_last_opinion(self):
        opinions = [_op(0, 9)]
        self.assertEqual(compute_coverage_gaps(opinions, 1, 12), [(11, 12, 2)])

    def test_multiple_gaps(self):
        opinions = [_op(2, 4), _op(8, 9)]
        self.assertEqual(
            compute_coverage_gaps(opinions, 1, 12),
            [(1, 2, 2), (6, 8, 3), (11, 12, 2)],
        )

    def test_start_page_offset_is_applied(self):
        opinions = [_op(0, 10)]
        self.assertEqual(
            compute_coverage_gaps(opinions, 100, 112), [(111, 112, 2)]
        )

    def test_returns_empty_for_missing_inputs(self):
        self.assertEqual(compute_coverage_gaps([], 1, 10), [])
        self.assertEqual(compute_coverage_gaps([_op(0, 3)], None, 10), [])
        self.assertEqual(compute_coverage_gaps([_op(0, 3)], 1, None), [])
