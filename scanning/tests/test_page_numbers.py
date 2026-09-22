"""Tests for the dots.mocr page-number adapter (issues #149/#204/#228).

Pure-function tests over hand-built page dicts of the glued volume
shape: no DB, no S3. The geometry matches the worker's defaults --
1700x2200px renders, so the head band ends at 187, the foot band
starts at 2090, and a token within 425px of its own edge is a corner
one.

The cells of the #228 tests are copied from a real run: scan run 1 of
469 P.3d, 1294 pages.
"""

from django.test import SimpleTestCase

from scanning import page_numbers

WIDTH, HEIGHT = 1700, 2200
HEAD_BBOX = [323, 143, 364, 177]
FOOT_BBOX = [800, 2120, 900, 2160]
BODY_BBOX = [200, 900, 1500, 1100]
#: A running head that spans the text block, so its two ends sit at
#: the same distance from their own edge of the page.
WIDE_HEAD_BBOX = [150, 143, 1550, 177]


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
        self.assertEqual(entry["score"], 1.0)

    def test_an_odd_page_number_trails_the_cite_line(self):
        entry = self.extract([cell("STATE v. SMITH Cite as 218 A.3d 677 679")])

        self.assertEqual(entry["detected"], "679")

    def test_the_nearer_end_of_a_line_wins(self):
        """Both ends carry a number, so the geometry separates them."""
        entry = self.extract([cell("12 SOMETHING 14")])
        self.assertEqual(entry["detected"], "12")

        entry = self.extract(
            [cell("12 SOMETHING 14", bbox=[1200, 143, 1550, 177])]
        )
        self.assertEqual(entry["detected"], "14")

    def test_a_range_is_read_whole(self):
        for text in ("677-685", "677–685", "677 - 685"):
            with self.subTest(text=text):
                entry = self.extract([cell(text)])

                self.assertEqual(entry["detected"], "677-685")
                self.assertEqual(entry["type"], "range")
                self.assertEqual(entry["score"], 1.0)

    def test_a_range_leads_the_running_head(self):
        """The compressed page of issue #233: the range is one token of
        a full head line, which the whole-line rule alone missed."""
        for text in (
            "913-925 ATLANTIC REPORTER, 2d SERIES",
            "913–925 ATLANTIC REPORTER, 2d SERIES",
        ):
            with self.subTest(text=text):
                entry = self.extract([cell(text)])

                self.assertEqual(entry["detected"], "913-925")
                self.assertEqual(entry["type"], "range")

    def test_a_range_trails_the_running_head(self):
        entry = self.extract(
            [
                cell(
                    "STATE v. SMITH Cite as 218 A.3d 913-925",
                    bbox=[1200, 143, 1550, 177],
                )
            ]
        )

        self.assertEqual(entry["detected"], "913-925")
        self.assertEqual(entry["type"], "range")

    def test_two_numbers_that_are_no_page_range(self):
        """A docket number runs past the end of any volume, and a split
        year runs backward. Neither is a page range."""
        for text in ("19-1234", "1996-97", "925-913", "0-5"):
            with self.subTest(text=text):
                self.assertIsNone(self.extract([cell(text)])["detected"])
                self.assertIsNone(
                    self.extract([cell(f"{text} A REPORTER")])["detected"]
                )

    def test_a_number_with_a_trailing_letter_is_read_whole(self):
        """The page the book adds between two numbered pages (#319)."""
        entry = self.extract([cell("2094a")])

        self.assertEqual(entry["detected"], "2094a")
        self.assertEqual(entry["type"], "suffixed")
        self.assertEqual(entry["score"], 1.0)

    def test_a_trailing_letter_leads_the_running_head(self):
        entry = self.extract([cell("2094a OCTOBER TERM, 2019")])

        self.assertEqual(entry["detected"], "2094a")
        self.assertEqual(entry["type"], "suffixed")

    def test_a_trailing_letter_trails_the_cite_line(self):
        entry = self.extract(
            [
                cell(
                    "SMITH v. JONES Cite as 140 S.Ct. 2094b",
                    bbox=[1200, 143, 1550, 177],
                )
            ]
        )

        self.assertEqual(entry["detected"], "2094b")
        self.assertEqual(entry["type"], "suffixed")

    def test_the_case_of_the_trailing_letter_is_kept(self):
        """The book prints one of the two glyphs, and no reader
        compares them."""
        entry = self.extract([cell("2094A")])

        self.assertEqual(entry["detected"], "2094A")
        self.assertEqual(entry["type"], "suffixed")

    def test_the_stray_icon_is_not_a_trailing_letter(self):
        """The parallel-page icon is misread as an ``L`` glued to the
        number (#228), so the plain number is tried first."""
        for text in ("2094L", "L2094"):
            with self.subTest(text=text):
                entry = self.extract([cell(text)])

                self.assertEqual(entry["detected"], "2094")
                self.assertEqual(entry["type"], "single")

    def test_only_the_first_six_letters_are_read(self):
        """The book labels the pages it adds in order from ``a``.

        Every other letter there is noise, and the icon of #228 is
        read as an ``l`` or an ``I``. The page keeps its
        ``no_page_number`` card, which a curator answers; a wrong
        suffixed reading would make no card at all (#319).
        """
        for text in ("2094a", "2094f", "2094A", "2094F"):
            with self.subTest(text=text):
                self.assertEqual(self.extract([cell(text)])["detected"], text)
        for text in ("2094l", "2094I", "2094O", "2094g", "2094z"):
            with self.subTest(text=text):
                self.assertIsNone(self.extract([cell(text)])["detected"])

    def test_a_series_ordinal_is_no_page_number(self):
        """``2d`` matches the shape exactly, so the reader asks for two
        digits (#319). A curator may still type ``9a``."""
        for text in ("2d", "3d", "9a"):
            with self.subTest(text=text):
                self.assertIsNone(self.extract([cell(text)])["detected"])

    def test_two_letters_are_no_page_number(self):
        for text in ("2094ab", "a2094", "2094a2"):
            with self.subTest(text=text):
                self.assertIsNone(self.extract([cell(text)])["detected"])

    def test_the_type_of_a_stored_number(self):
        """The one deriver every writer of a curator's number calls."""
        self.assertIsNone(page_numbers.number_type(""))
        self.assertIsNone(page_numbers.number_type(None))
        self.assertEqual(page_numbers.number_type("2094"), "single")
        self.assertEqual(page_numbers.number_type("2094a"), "suffixed")
        self.assertEqual(page_numbers.number_type("9a"), "suffixed")
        self.assertEqual(page_numbers.number_type("678-686"), "range")

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

    def test_a_band_only_corner_token_loses_a_signal(self):
        entry = self.extract([cell("678 ATLANTIC REPORTER", "Text")])

        self.assertEqual(entry["detected"], "678")
        self.assertEqual(entry["score"], 0.8)

    def test_a_bare_digit_away_from_the_edges_is_not_full_marks(self):
        """The parallel citation page of scan run 1, page 732.

        The label, the band and the digits agree, which was the full
        1.0 before #228. Only its position says it is not the page
        number, and only the rank acts on that.
        """
        entry = self.extract([cell("115", bbox=[1200, 114, 1249, 147])])

        self.assertEqual(entry["detected"], "115")
        self.assertEqual(entry["score"], 0.8)

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


class TestTheCornerWins(SimpleTestCase):
    """The printed number is at the outer corner, every rival is not.

    Each test carries the cells of one page of scan run 1 (469 P.3d).
    Before #228 the cell order decided all of them.
    """

    def extract(self, cells, pdf_page: int = 1):
        return page_numbers.extract_page_number(make_page(pdf_page, cells))

    def test_the_volume_number_loses_to_the_page_number(self):
        """Page 2: the reporter title leads with the volume number."""
        entry = self.extract(
            [
                cell("2 Idaho", bbox=[285, 95, 406, 130]),
                cell(
                    "469 PACIFIC REPORTER, 3d SERIES",
                    bbox=[584, 97, 1081, 128],
                ),
            ],
            pdf_page=2,
        )

        self.assertEqual(entry["detected"], "2")

    def test_the_parallel_citation_page_loses_to_the_page_number(self):
        """Page 732: '115' sits alone in a cell of its own."""
        entry = self.extract(
            [
                cell("732 Okl.", bbox=[274, 112, 411, 147]),
                cell(
                    "469 PACIFIC REPORTER, 3d SERIES",
                    bbox=[581, 114, 1113, 147],
                ),
                cell("115", bbox=[1200, 114, 1249, 147]),
            ],
            pdf_page=732,
        )

        self.assertEqual(entry["detected"], "732")

    def test_a_headnote_number_loses_to_the_page_number(self):
        """Page 105: dots labels a headnote number Page-header too.

        The headnote is a bare digit with the label and the band, which
        scored the full 1.0 before #228 and beat the true corner cell.
        """
        entry = self.extract(
            [
                cell("Kan. 105", bbox=[1291, 104, 1439, 140]),
                cell("1", bbox=[556, 174, 569, 196]),
                cell("3", bbox=[1149, 174, 1162, 196]),
            ],
            pdf_page=105,
        )

        self.assertEqual(entry["detected"], "105")

    def test_a_case_name_that_ends_in_a_digit_loses(self):
        """Page 743: 'SCHOOL DIST. NO. 1' ends in a number."""
        entry = self.extract(
            [
                cell(
                    "ALBURTUS v. INDEPENDENT SCHOOL DIST. NO. 1",
                    bbox=[472, 107, 1219, 139],
                ),
                cell("Okl. 743", bbox=[1285, 107, 1427, 144]),
            ],
            pdf_page=743,
        )

        self.assertEqual(entry["detected"], "743")

    def test_a_labelled_body_cell_loses_to_the_head_band(self):
        """dots labels a headnote number Page-header wherever it is.

        This one is printed in the margin of the body, nearer its edge
        than the running head is to its own, so distance alone would
        hand it the page. The band is what separates them, and the
        score is where the band is counted.
        """
        entry = self.extract(
            [
                cell("Okl. 743", bbox=[1285, 107, 1427, 144]),
                cell("2", bbox=[250, 900, 265, 925]),
            ],
            pdf_page=743,
        )

        self.assertEqual(entry["detected"], "743")

    def test_a_head_cell_of_two_lines_is_read_line_by_line(self):
        """Page 137: dots returns the head and the Cite line as one."""
        entry = self.extract(
            [
                cell(
                    "MOUNTAIN WATER v. MONTANA DEPT. OF REVENUE Mont. 137\n"
                    "Cite as 469 P.3d 316 (Mont. 2020)",
                    bbox=[447, 104, 1442, 165],
                )
            ],
            pdf_page=137,
        )

        self.assertEqual(entry["detected"], "137")

    def test_the_head_line_outranks_the_cite_line_below_it(self):
        """The two lines share one bbox, so the line order decides."""
        entry = self.extract(
            [
                cell(
                    "MOUNTAIN WATER v. MONTANA DEPT. OF REVENUE Mont. 137\n"
                    "Cite as 469 P.3d 316",
                    bbox=[447, 104, 1442, 165],
                )
            ],
            pdf_page=137,
        )

        self.assertEqual(entry["detected"], "137")


class TestOcrResultsFromVolume(SimpleTestCase):
    def test_entries_come_out_in_page_order(self):
        document = {
            "pages": [
                make_page(2, [cell("678")]),
                make_page(1, [cell("677")]),
            ]
        }

        results = page_numbers.ocr_results_from_volume(document)

        self.assertEqual(
            [(r["pdf_page"], r["detected"]) for r in results],
            [(1, "677"), (2, "678")],
        )

    def test_a_neighbour_resolves_two_readings_of_one_line(self):
        """The citation page and the page number, at one distance.

        Both ends of the middle page's head cell sit the same distance
        from their own edge, and both agree with the printed parity, so
        the geometry cannot separate them. The page before decides.
        """
        document = {
            "pages": [
                make_page(1, [cell("Ky. 100", bbox=[1285, 107, 1427, 144])]),
                make_page(
                    2,
                    [cell("90 SMITH v. JONES 101", bbox=WIDE_HEAD_BBOX)],
                ),
                make_page(3, [cell("Ky. 102", bbox=[1285, 107, 1427, 144])]),
            ]
        }

        results = page_numbers.ocr_results_from_volume(document)

        self.assertEqual(
            [r["detected"] for r in results], ["100", "101", "102"]
        )

    def test_an_uncontested_reading_is_the_geometry_s_to_keep(self):
        """The pass never overrules a page that offers one number."""
        document = {
            "pages": [
                make_page(1, [cell("Ky. 100", bbox=[1285, 107, 1427, 144])]),
                make_page(2, [cell("Ky. 150", bbox=[1285, 107, 1427, 144])]),
                make_page(3, [cell("Ky. 102", bbox=[1285, 107, 1427, 144])]),
            ]
        }

        results = page_numbers.ocr_results_from_volume(document)

        self.assertEqual(results[1]["detected"], "150")

    def test_a_column_of_headnote_numbers_is_not_a_sequence(self):
        """The rivals of a page number run in sequence themselves.

        A headnote column counts 1, 2, 3 down the volume, so a pass
        that trusted the sequence over the band would read it as the
        page numbers and approve itself.
        """
        document = {
            "pages": [
                make_page(
                    1,
                    [
                        cell("Okl. 741", bbox=[1285, 107, 1427, 144]),
                        cell("1", bbox=[250, 900, 265, 925]),
                    ],
                ),
                make_page(
                    2,
                    [
                        cell("742 Okl.", bbox=[274, 112, 411, 147]),
                        cell("2", bbox=[250, 1100, 265, 1125]),
                    ],
                ),
                make_page(
                    3,
                    [
                        cell("Okl. 743", bbox=[1285, 107, 1427, 144]),
                        cell("3", bbox=[250, 1300, 265, 1325]),
                    ],
                ),
            ]
        }

        results = page_numbers.ocr_results_from_volume(document)

        self.assertEqual(
            [r["detected"] for r in results], ["741", "742", "743"]
        )

    def test_one_neighbour_alone_never_moves_a_pick(self):
        """A misread page must not hand its sequence to the next one.

        Page 1 reads a rival, and page 2 offers the number that
        continues it. Only page 3 could confirm that, and it has no
        number at all, so page 2 keeps what the geometry read.
        """
        document = {
            "pages": [
                make_page(1, [cell("Ky. 500", bbox=[1285, 107, 1427, 144])]),
                make_page(
                    2,
                    [
                        cell("Ky. 101", bbox=[1285, 107, 1427, 144]),
                        cell("501", bbox=[1200, 114, 1249, 147]),
                    ],
                ),
                make_page(3, None),
            ]
        }

        results = page_numbers.ocr_results_from_volume(document)

        self.assertEqual(results[1]["detected"], "101")

    def test_a_range_is_never_moved(self):
        """A range names two pages, so it answers no sequence."""
        document = {
            "pages": [
                make_page(1, [cell("Ky. 100", bbox=[1285, 107, 1427, 144])]),
                make_page(
                    2,
                    [
                        cell("101-109", bbox=[1285, 107, 1427, 144]),
                        cell(
                            "101", "Page-footer", bbox=[800, 2120, 900, 2160]
                        ),
                    ],
                ),
                make_page(3, [cell("Ky. 102", bbox=[1285, 107, 1427, 144])]),
            ]
        }

        results = page_numbers.ocr_results_from_volume(document)

        self.assertEqual(results[1]["detected"], "101-109")
        self.assertEqual(results[1]["type"], "range")

    def test_the_pass_never_invents_a_number(self):
        document = {
            "pages": [
                make_page(1, [cell("Ky. 100", bbox=[1285, 107, 1427, 144])]),
                make_page(2, None),
                make_page(3, [cell("Ky. 102", bbox=[1285, 107, 1427, 144])]),
            ]
        }

        results = page_numbers.ocr_results_from_volume(document)

        self.assertIsNone(results[1]["detected"])

    def test_the_output_is_the_run_and_nothing_else(self):
        # Pure machine output since #214: a curator's own number is a
        # PageEdit row, overlaid on top of this by page_edits.
        document = {"pages": [make_page(1, [cell("677")])]}

        results = page_numbers.ocr_results_from_volume(document)

        self.assertEqual(results[0]["detected"], "677")
        self.assertEqual(results[0]["zone"], "dots-header")


class TestCarriesNumber(SimpleTestCase):
    """The reader of the OCR glue's page-number verdict (#396)."""

    def test_the_number_at_the_start_of_the_running_head(self):
        self.assertTrue(page_numbers.carries_number("878 N. C.", "878"))

    def test_the_number_at_the_end_of_the_cite_as_line(self):
        text = "STATE v. SMITH\nCite as 218 A.3d 677 -- 679"
        self.assertTrue(page_numbers.carries_number(text, "679"))

    def test_a_number_buried_in_the_line_is_no_reading(self):
        text = "STATE v. SMITH\nCite as 218 A.3d 677 -- 679"
        self.assertFalse(page_numbers.carries_number(text, "218"))
        self.assertFalse(page_numbers.carries_number(text, "677"))

    def test_the_first_page_carries_its_own_number_in_the_cite_line(self):
        """On the first page of an opinion the ``Cite as`` line ends in
        the page's own number."""
        self.assertTrue(
            page_numbers.carries_number("Cite as 218 A.3d 677", "677")
        )

    def test_another_number_is_not_the_page_number(self):
        self.assertFalse(page_numbers.carries_number("877 N. C.", "878"))

    def test_a_range_and_a_suffixed_number(self):
        self.assertTrue(
            page_numbers.carries_number(
                "913–925 ATLANTIC REPORTER, 2d SERIES", "913-925"
            )
        )
        self.assertTrue(
            page_numbers.carries_number("2094a ATLANTIC REPORTER", "2094a")
        )

    def test_the_stray_l_and_a_superscript_are_forgiven(self):
        self.assertTrue(page_numbers.carries_number("878L N. C.", "878"))
        self.assertTrue(page_numbers.carries_number("878¹ N. C.", "878"))

    def test_no_value_and_no_text_read_as_nothing(self):
        self.assertFalse(page_numbers.carries_number("878 N. C.", None))
        self.assertFalse(page_numbers.carries_number("878 N. C.", ""))
        self.assertFalse(page_numbers.carries_number("", "878"))

    def test_a_body_paragraph_that_ends_in_the_number(self):
        """The reader answers the text alone; the glue adds the band,
        which is what keeps this paragraph in the text."""
        self.assertTrue(
            page_numbers.carries_number("the court said, at 878", "878")
        )


class TestBandOf(SimpleTestCase):
    """One band rule for a render box and for a box in points."""

    def test_the_head_the_foot_and_the_body_in_points(self):
        self.assertEqual(
            page_numbers.band_of([36, 18, 300, 43], 792), "header"
        )
        self.assertEqual(
            page_numbers.band_of([36, 760, 300, 780], 792), "footer"
        )
        self.assertIsNone(page_numbers.band_of([36, 100, 300, 700], 792))

    def test_a_box_that_reaches_below_the_band_is_the_body(self):
        self.assertIsNone(page_numbers.band_of([36, 18, 300, 200], 792))

    def test_no_geometry_is_the_body(self):
        self.assertIsNone(page_numbers.band_of([], 792))
        self.assertIsNone(page_numbers.band_of([36, 18, 300, 43], 0))
