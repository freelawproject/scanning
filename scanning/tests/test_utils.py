"""Tests for scanning.utils helpers."""

import pathlib
import tempfile
from unittest.mock import patch

from django.test import TestCase, override_settings

from scanning.factories import ScanFactory
from scanning.utils import compute_coverage_gaps, local_original_pdf

MEDIA_ROOT = tempfile.mkdtemp()


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


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestLocalOriginalPdf(TestCase):
    """Resolving the original PDF falls back to a targeted S3 pull.

    Production keeps the (multi-GB) original in S3 only, so request
    handlers that need it must tolerate a pod that never downloaded it
    rather than raising FileNotFoundError (SCANNING-1S).
    """

    def _make_scan(self):
        """Create a scan whose original PDF is absent locally, as in prod."""
        scan = ScanFactory()
        pathlib.Path(scan.original_pdf.path).unlink()
        return scan

    def test_returns_path_when_present(self):
        """With a local copy the path is returned without touching S3."""
        scan = ScanFactory()
        with patch("scanning.s3_sync.download_original_pdf") as download:
            self.assertEqual(local_original_pdf(scan), scan.original_pdf.path)
        download.assert_not_called()

    def test_pulls_from_s3_when_missing(self):
        """A missing original triggers a pull and the retried path wins."""
        scan = self._make_scan()
        landed = (
            pathlib.Path(scan.output_dir)
            / pathlib.Path(scan.original_pdf.name).name
        )

        def _land(_scan):
            landed.parent.mkdir(parents=True, exist_ok=True)
            landed.write_bytes(b"%PDF-1.4 pulled")

        with patch(
            "scanning.s3_sync.download_original_pdf", side_effect=_land
        ) as download:
            self.assertEqual(local_original_pdf(scan), str(landed))
        download.assert_called_once_with(scan)

    def test_returns_none_when_pull_finds_nothing(self):
        """A pull that lands no file yields None, not an exception."""
        scan = self._make_scan()
        with patch(
            "scanning.s3_sync.download_original_pdf", return_value=None
        ):
            self.assertIsNone(local_original_pdf(scan))

    def test_returns_none_when_pull_raises(self):
        """An S3 error is logged and reported as None, not propagated."""
        scan = self._make_scan()
        with patch(
            "scanning.s3_sync.download_original_pdf",
            side_effect=OSError("s3 down"),
        ):
            self.assertIsNone(local_original_pdf(scan))
