"""Tests for the dots.mocr page-number adapter (issues #149/#204).

Pure-function tests over hand-built page dicts of the glued volume
shape: no DB, no S3. The geometry matches the worker's defaults --
2200px render height, so the head band ends at 187 and the foot band
starts at 2090.
"""

from django.test import SimpleTestCase

from scanning import page_numbers

WIDTH, HEIGHT = 1700, 2200
HEAD_BBOX = [323, 143, 364, 177]
FOOT_BBOX = [800, 2120, 900, 2160]
BODY_BBOX = [200, 900, 1500, 1100]


def make_page(pdf_page: int, cells: list | None, **overrides) -> dict:
    """Build one page entry of the glued volume document.

    :param pdf_page: 1-based page number in the PDF.
    :param cells: The page's layout cells, or None for a filtered page.
    :param overrides: Page fields to replace.
    :returns: A page dict.
    :rtype: dict
    """
    page = {
        "pdf_page": pdf_page,
        "page_index": pdf_page - 1,
        "shard_index": 0,
        "page_no": pdf_page - 1,
        "origin_width": WIDTH,
        "origin_height": HEIGHT,
        "filtered": cells is None,
        "cells": cells,
        "md": "",
    }
    page.update(overrides)
    return page


def cell(text: str, category: str = "Page-header", bbox: list | None = None):
    """Build one dots layout cell.

    :param text: The cell's text.
    :param category: The dots label.
    :param bbox: The cell's bbox; the head-band one when omitted.
    :returns: A cell dict.
    :rtype: dict
    """
    return {"bbox": bbox or HEAD_BBOX, "category": category, "text": text}


class TestExtractPageNumber(SimpleTestCase):
    def extract(self, cells, **overrides):
        return page_numbers.extract_page_number(
            make_page(1, cells, **overrides)
        )

    def test_a_digits_only_header_cell_scores_full(self):
        """All three signals agree: label, band, and a bare number."""
        entry = self.extract([cell("677")])

        self.assertEqual(entry["detected"], "677")
        self.assertEqual(entry["type"], "single")
        self.assertEqual(entry["score"], 1.0)
        self.assertEqual(entry["zone"], "dots-header")
        self.assertEqual(entry["ocr"], "677")
        self.assertEqual(entry["img_width"], WIDTH)
        self.assertEqual(entry["img_height"], HEIGHT)
        self.assertEqual(entry["pdf_page"], 1)

    def test_an_even_page_number_leads_the_running_head(self):
        entry = self.extract([cell("678 ATLANTIC REPORTER, 2d SERIES")])

        self.assertEqual(entry["detected"], "678")
        self.assertEqual(entry["type"], "single")
        self.assertEqual(entry["score"], 0.8)

    def test_an_odd_page_number_trails_the_cite_line(self):
        entry = self.extract(
            [cell("STATE v. SMITH Cite as 218 A.3d 677 679")]
        )

        self.assertEqual(entry["detected"], "679")

    def test_numbers_at_both_ends_break_the_tie_on_parity(self):
        """An odd number sits on a right page's trailing corner."""
        entry = self.extract([cell("10 SOMETHING 11")])
        self.assertEqual(entry["detected"], "11")

        entry = self.extract([cell("11 SOMETHING 10")])
        self.assertEqual(entry["detected"], "11")

    def test_a_range_is_read_whole(self):
        for text in ("677-685", "677–685"):
            with self.subTest(text=text):
                entry = self.extract([cell(text)])

                self.assertEqual(entry["detected"], "677-685")
                self.assertEqual(entry["type"], "range")
                self.assertEqual(entry["score"], 1.0)

    def test_a_section_opening_page_reads_its_footer(self):
        entry = self.extract(
            [
                cell("ATLANTIC REPORTER", "Page-header"),
                cell("677", "Page-footer", FOOT_BBOX),
            ]
        )

        self.assertEqual(entry["detected"], "677")
        self.assertEqual(entry["zone"], "dots-footer")

    def test_a_header_with_a_number_outranks_the_footer(self):
        entry = self.extract(
            [
                cell("123", "Page-footer", FOOT_BBOX),
                cell("677", "Page-header"),
            ]
        )

        self.assertEqual(entry["detected"], "677")
        self.assertEqual(entry["zone"], "dots-header")

    def test_the_stray_parallel_page_icon_is_stripped(self):
        """dots misreads the parallel-page icon as an ``L``."""
        for text in ("L677", "677L"):
            with self.subTest(text=text):
                entry = self.extract([cell(text)])
                self.assertEqual(entry["detected"], "677")

    def test_superscript_digits_are_noise(self):
        entry = self.extract([cell("⁵677")])
        self.assertEqual(entry["detected"], "677")

    def test_a_body_cell_never_votes(self):
        """A year in the text is neither labeled nor in a band."""
        entry = self.extract([cell("1994", "Text", BODY_BBOX)])
        self.assertIsNone(entry["detected"])

    def test_an_unlabeled_cell_in_the_band_still_counts(self):
        entry = self.extract([cell("677", "Text")])

        self.assertEqual(entry["detected"], "677")
        self.assertEqual(entry["score"], 0.8)
        self.assertEqual(entry["zone"], "dots-header")

    def test_a_band_only_corner_token_scores_lowest(self):
        entry = self.extract([cell("678 ATLANTIC REPORTER", "Text")])

        self.assertEqual(entry["detected"], "678")
        self.assertEqual(entry["score"], 0.5)

    def test_a_labeled_cell_out_of_band_still_counts(self):
        entry = self.extract([cell("677", "Page-header", BODY_BBOX)])

        self.assertEqual(entry["detected"], "677")
        self.assertEqual(entry["score"], 0.8)

    def test_a_number_buried_mid_text_is_not_trusted(self):
        entry = self.extract([cell("ATLANTIC 677 REPORTER")])
        self.assertIsNone(entry["detected"])

    def test_a_headline_without_numbers_reads_as_none(self):
        entry = self.extract([cell("ATLANTIC REPORTER")])

        self.assertIsNone(entry["detected"])
        self.assertIsNone(entry["type"])
        self.assertIsNone(entry["score"])
        self.assertIsNone(entry["zone"])
        self.assertIsNone(entry["ocr"])

    def test_a_filtered_page_reads_as_none(self):
        entry = self.extract(None)
        self.assertIsNone(entry["detected"])

    def test_a_failed_page_reads_as_none(self):
        entry = page_numbers.extract_page_number(
            {
                "pdf_page": 3,
                "page_index": 2,
                "shard_index": 1,
                "page_no": 0,
                "error": "boom",
            }
        )

        self.assertIsNone(entry["detected"])
        self.assertIsNone(entry["img_width"])
        self.assertEqual(entry["pdf_page"], 3)

    def test_five_digit_tokens_are_not_page_numbers(self):
        entry = self.extract([cell("12345")])
        self.assertIsNone(entry["detected"])


class TestOcrResultsFromVolume(SimpleTestCase):
    def test_entries_come_out_in_page_order(self):
        document = {
            "pages": [
                make_page(2, [cell("678")]),
                make_page(1, [cell("677")]),
            ]
        }

        results = page_numbers.ocr_results_from_volume(document, None)

        self.assertEqual(
            [(r["pdf_page"], r["detected"]) for r in results],
            [(1, "677"), (2, "678")],
        )

    def test_a_manual_entry_is_kept_verbatim(self):
        manual = {
            "pdf_page": 1,
            "detected": "9",
            "type": "single",
            "score": 1.0,
            "zone": "manual",
            "ocr": "manual",
            "img_width": WIDTH,
            "img_height": HEIGHT,
        }
        document = {"pages": [make_page(1, [cell("677")])]}

        results = page_numbers.ocr_results_from_volume(document, [manual])

        self.assertEqual(results, [manual])

    def test_a_model_entry_is_replaced(self):
        stale = {"pdf_page": 1, "detected": "9", "zone": "dots-header"}
        document = {"pages": [make_page(1, [cell("677")])]}

        results = page_numbers.ocr_results_from_volume(document, [stale])

        self.assertEqual(results[0]["detected"], "677")
