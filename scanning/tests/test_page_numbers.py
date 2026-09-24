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

from scanning import opinion_ocr, page_numbers

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
        entry = self.extract(
            [
                cell(
                    "STATE v. SMITH Cite as 218 A.3d 677 679",
                    bbox=[1200, 143, 1550, 177],
                )
            ]
        )

        self.assertEqual(entry["detected"], "679")

    def test_a_word_of_a_line_that_stays_off_the_corner_is_no_number(self):
        """The running head of a left page whose own number dots.mocr
        dropped (#351): its first word is the volume number, and the
        cell starts a quarter of the page in. Before the guard it was
        read as the page number, at half marks, on every such page."""
        entry = self.extract(
            [
                cell(
                    "992 FEDERAL REPORTER, 3d SERIES",
                    bbox=[465, 64, 1132, 101],
                )
            ]
        )

        self.assertIsNone(entry["detected"])

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
        """The reader answers the text alone; the glue adds the zone,
        which is what keeps this paragraph in the text."""
        self.assertTrue(
            page_numbers.carries_number("the court said, at 878", "878")
        )

    def test_the_marks_of_the_engines_are_folded_first(self):
        """Mistral and dots.mocr set heading and bold marks around the
        head, Surya a bullet. None of them is a token."""
        for text in (
            "# 878 N. C.",
            "## 878 N. C.",
            "**878 N. C.**",
            "• 878 N. C.",
            "**878** N. C.",
        ):
            self.assertTrue(page_numbers.carries_number(text, "878"), text)
        self.assertTrue(
            page_numbers.carries_number(
                "Cite as 218 A.3d 677 -- **679**", "679"
            )
        )
        self.assertTrue(
            page_numbers.carries_number("# Cite as 218 A.3d 677 -- 679", "679")
        )
        self.assertFalse(
            page_numbers.carries_number("# Cite as 218 A.3d 677 -- 679", "218")
        )


class TestIsHeadOrFootLabel(SimpleTestCase):
    """The label rule beside the band rule (#396)."""

    def test_the_labels_of_the_engine_table(self):
        """dots.mocr's, Surya's and Mistral's own spellings, off
        ``EngineSpec.band_labels`` (#351)."""
        for label in (
            "Page-header",
            "Page-footer",
            "PageHeader",
            "PageFooter",
            "header",
        ):
            self.assertTrue(page_numbers.is_head_or_foot_label(label), label)

    def test_mistral_s_footer_is_footnote_text(self):
        """The label holds footnotes on the pages measured (#399), and a
        footnote line that ends in a page number must stay in the text,
        so the foot of a Mistral page is judged by its band alone."""
        self.assertFalse(page_numbers.is_head_or_foot_label("footer"))

    def test_every_other_label_is_the_body(self):
        for label in (
            "Text",
            "SectionHeader",
            "Section-header",
            "text",
            "",
            None,
        ):
            self.assertFalse(page_numbers.is_head_or_foot_label(label), label)


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


# ── the other engines (#351) ────────────────────────────────────────
MISTRAL_RENDER = {"width": 1700, "height": 2200, "source": "original"}


def mistral_block(
    text: str, kind: str = "header", bbox: list | None = None
) -> dict:
    """Build one block of a glued Mistral volume document.

    :param text: The block's text.
    :param kind: The Mistral ``type``.
    :param bbox: The box, in the 1700x2200 render; a corner header box
        when omitted. None stays None, the shape of a block Mistral
        wrote no corners for.
    :returns: A block dict.
    :rtype: dict
    """
    if bbox is None:
        bbox = [112.0, 66.0, 143.0, 104.0]
    return {"id": 0, "type": kind, "bbox": bbox or None, "content": text}


def mistral_document(pages: dict[int, list[dict]]) -> dict:
    """Build a glued Mistral volume document.

    :param pages: ``{pdf_page: blocks}``.
    :returns: The document.
    :rtype: dict
    """
    return {
        "engine": "mistral_ocr",
        "render": MISTRAL_RENDER,
        "pages": [
            {
                "page_index": pdf_page - 1,
                "pdf_page": pdf_page,
                "md": "",
                "blocks": blocks,
                "dimensions": {"dpi": 200, "height": 2200, "width": 1700},
            }
            for pdf_page, blocks in sorted(pages.items())
        ],
    }


def surya_block(
    text: str, label: str = "PageHeader", bbox: list | None = None
) -> dict:
    """Build one block of a glued Surya volume document.

    :param text: The block's text.
    :param label: The Surya ``label``.
    :param bbox: The box, in the page's own render; a corner header box
        when omitted.
    :returns: A block dict.
    :rtype: dict
    """
    return {
        "order": 0,
        "label": label,
        "raw_label": label.lower(),
        "bbox": bbox or [1179.83, 68.04, 1202.69, 101.09],
        "confidence": 0.99,
        "html": f"<p>{text}</p>",
        "text": text,
        "skipped": False,
        "error": False,
    }


def surya_document(pages: dict[int, list[dict]]) -> dict:
    """Build a glued Surya volume document.

    :param pages: ``{pdf_page: blocks}``.
    :returns: The document.
    :rtype: dict
    """
    return {
        "engine": "surya",
        "pages": [
            {
                "page_index": pdf_page - 1,
                "pdf_page": pdf_page,
                "origin_width": 1270,
                "origin_height": 1944,
                "blocks": blocks,
                "text": "",
            }
            for pdf_page, blocks in sorted(pages.items())
        ],
    }


#: The running head of a left page, as dots.mocr writes it when it
#: drops the corner number: the one cell of the head band.
RUNNING_HEAD = cell("992 FEDERAL REPORTER, 3d SERIES", bbox=[357, 59, 864, 84])


class TestTheOtherEnginesFillTheBlanks(SimpleTestCase):
    """A page dots.mocr left blank takes the reading of another engine.

    The pages are dots.mocr's, at its own render (1302x1944 here, the
    render of the volume of #351); each other engine's document is in
    its own render, and the geometry reads each in its own.
    """

    def read(self, cells_by_page: dict[int, list | None], **fallbacks):
        """Read a volume of dots.mocr pages with the given fallbacks.

        :param cells_by_page: ``{pdf_page: cells}`` of the dots.mocr
            document.
        :param fallbacks: ``mistral_ocr=`` and ``surya=`` documents.
        :returns: ``Scan.ocr_results``.
        """
        document = {
            "pages": [
                make_page(
                    pdf_page, cells, origin_width=1302, origin_height=1944
                )
                for pdf_page, cells in cells_by_page.items()
            ]
        }
        return page_numbers.ocr_results_from_volume(document, fallbacks)

    def test_a_page_dots_left_blank_takes_the_mistral_header(self):
        alone = self.read({2: [RUNNING_HEAD]})
        self.assertIsNone(alone[0]["detected"])

        filled = self.read(
            {2: [RUNNING_HEAD]},
            mistral_ocr=mistral_document(
                {
                    2: [
                        mistral_block(
                            "992 FEDERAL REPORTER, 3d SERIES",
                            bbox=[465.0, 64.0, 1132.0, 101.0],
                        ),
                        mistral_block("2"),
                    ]
                }
            ),
        )

        entry = filled[0]
        self.assertEqual(entry["detected"], "2")
        self.assertEqual(entry["type"], "single")
        self.assertEqual(entry["score"], 1.0)
        self.assertEqual(entry["zone"], "mistral-header")
        self.assertEqual(entry["ocr"], "2")
        # The entry is still dots.mocr's page: its render, its number.
        self.assertEqual(entry["pdf_page"], 2)
        self.assertEqual(
            (entry["img_width"], entry["img_height"]), (1302, 1944)
        )

    def test_a_page_dots_left_blank_takes_the_surya_header(self):
        filled = self.read(
            {
                3: [
                    cell(
                        "EMMANUEL v. HANDY TECHNOLOGIES, INC.",
                        bbox=[376, 73, 979, 98],
                    )
                ]
            },
            surya=surya_document(
                {
                    3: [
                        surya_block(
                            "EMMANUEL v. HANDY TECHNOLOGIES, INC.",
                            bbox=[370.84, 66.1, 985.52, 99.14],
                        ),
                        surya_block("3"),
                    ]
                }
            ),
        )

        self.assertEqual(filled[0]["detected"], "3")
        self.assertEqual(filled[0]["zone"], "surya-header")

    def test_the_dots_reading_stands_where_it_has_one(self):
        """The read every volume pays for, and the one every rule was
        measured on: another engine fills, it never overrules."""
        results = self.read(
            {15: [cell("15", bbox=[1166, 71, 1198, 99])]},
            mistral_ocr=mistral_document({15: [mistral_block("16")]}),
        )

        self.assertEqual(results[0]["detected"], "15")
        self.assertEqual(results[0]["zone"], "dots-header")

    def test_the_first_engine_of_the_table_answers_first(self):
        results = self.read(
            {2: [RUNNING_HEAD]},
            surya=surya_document({2: [surya_block("22")]}),
            mistral_ocr=mistral_document({2: [mistral_block("2")]}),
        )

        self.assertEqual(results[0]["detected"], "2")
        self.assertEqual(results[0]["zone"], "mistral-header")
        self.assertEqual(
            list(opinion_ocr.ENGINES), ["dots_mocr", "mistral_ocr", "surya"]
        )

    def test_the_neighbour_pass_overrules_a_dots_pick(self):
        """The one case another engine overrules the primary read.
        Both neighbours ask for 11; dots.mocr read the parallel
        citation page of page 2, and Mistral read the number."""
        results = self.read(
            {
                1: [cell("10", bbox=[70, 60, 100, 90])],
                2: [cell("115", bbox=[1100, 60, 1140, 90])],
                3: [cell("12", bbox=[1200, 60, 1230, 90])],
            },
            mistral_ocr=mistral_document(
                {2: [mistral_block("11", bbox=[1560.0, 79.0, 1615.0, 117.0])]}
            ),
        )

        self.assertEqual(
            [(r["detected"], r["zone"]) for r in results],
            [
                ("10", "dots-header"),
                ("11", "mistral-header"),
                ("12", "dots-header"),
            ],
        )

    def test_a_fallback_running_head_is_no_number_either(self):
        """The guard holds for every engine: the volume number leads
        Mistral's header block too, a quarter of the page in."""
        results = self.read(
            {2: [RUNNING_HEAD]},
            mistral_ocr=mistral_document(
                {
                    2: [
                        mistral_block(
                            "992 FEDERAL REPORTER, 3d SERIES",
                            bbox=[465.0, 64.0, 1132.0, 101.0],
                        )
                    ]
                }
            ),
        )

        self.assertIsNone(results[0]["detected"])

    def test_a_block_with_no_box_reads_a_whole_line_number_at_half_marks(self):
        """A block Mistral wrote no corners for has no band and no
        corner: the label alone carries a bare number."""
        results = self.read(
            {2: [RUNNING_HEAD]},
            mistral_ocr=mistral_document({2: [mistral_block("2", bbox=[])]}),
        )

        self.assertEqual(results[0]["detected"], "2")
        self.assertEqual(results[0]["score"], 0.5)
        self.assertIsNone(
            self.read(
                {2: [RUNNING_HEAD]},
                mistral_ocr=mistral_document(
                    {2: [mistral_block("2", kind="text", bbox=[])]}
                ),
            )[0]["detected"]
        )

    def test_the_marks_of_the_engines_are_folded_first(self):
        """Mistral and dots.mocr set heading and bold marks around a
        head line, Surya a bullet (#396): none of them is a token, or
        the number would be read in the middle of its line."""
        for text in ("# 2", "**2**", "# 2 FEDERAL REPORTER, 3d SERIES"):
            with self.subTest(text=text):
                results = self.read(
                    {2: [RUNNING_HEAD]},
                    mistral_ocr=mistral_document({2: [mistral_block(text)]}),
                )
                self.assertEqual(results[0]["detected"], "2")
                self.assertEqual(results[0]["ocr"], text)
        results = self.read(
            {3: [RUNNING_HEAD]},
            surya=surya_document({3: [surya_block("• 3")]}),
        )
        self.assertEqual(results[0]["detected"], "3")

    def test_a_page_with_no_width_keeps_the_reading_before_the_gate(self):
        """The gate is about the reporter title a quarter of the page
        in; a malformed page has no corner to measure."""
        entry = page_numbers.extract_page_number(
            make_page(
                2,
                [cell("2 FEDERAL REPORTER", bbox=[357, 59, 864, 84])],
                origin_width=0,
            )
        )

        self.assertEqual(entry["detected"], "2")
        self.assertEqual(entry["score"], 0.5)

    def test_the_pages_are_dots_pages(self):
        """An engine's page the dots.mocr document does not hold
        answers nothing, and a dots.mocr page it lacks stays blank."""
        results = self.read(
            {1: [RUNNING_HEAD], 2: [RUNNING_HEAD]},
            mistral_ocr=mistral_document(
                {2: [mistral_block("2")], 99: [mistral_block("99")]}
            ),
        )

        self.assertEqual(
            [(r["pdf_page"], r["detected"]) for r in results],
            [(1, None), (2, "2")],
        )

    def test_a_fallback_with_no_pages_is_ignored(self):
        for fallbacks in (
            {},
            {"mistral_ocr": None},
            {"mistral_ocr": {}},
            {"mistral_ocr": {"pages": "not a list"}},
            {"not_an_engine": mistral_document({2: [mistral_block("2")]})},
        ):
            with self.subTest(fallbacks=fallbacks):
                results = self.read({2: [RUNNING_HEAD]}, **fallbacks)
                self.assertIsNone(results[0]["detected"])

    def test_the_primary_engine_is_the_first_of_the_table(self):
        self.assertEqual(page_numbers.PRIMARY_ENGINE, "dots_mocr")
        self.assertEqual(
            page_numbers.PRIMARY_ENGINE, opinion_ocr.DEFAULT_ENGINE
        )


class TestTheEngineTable(SimpleTestCase):
    """What the reader needs of every entry of ``opinion_ocr.ENGINES``."""

    def test_every_engine_names_the_head(self):
        """The foot too where the engine has a label for it: Mistral's
        ``footer`` is footnote text and is left out."""
        for name, spec in opinion_ocr.ENGINES.items():
            with self.subTest(engine=name):
                self.assertIn("header", spec.band_labels.values())
                self.assertLessEqual(
                    set(spec.band_labels.values()), {"header", "footer"}
                )

    def test_every_engine_stamps_its_own_zone(self):
        prefixes = [spec.zone_prefix for spec in opinion_ocr.ENGINES.values()]

        self.assertEqual(len(set(prefixes)), len(prefixes))
        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                self.assertTrue(prefix.endswith("-"))
                self.assertTrue(page_numbers.is_model_zone(f"{prefix}header"))
        self.assertEqual(opinion_ocr.ENGINES["dots_mocr"].zone_prefix, "dots-")

    def test_a_curator_s_zone_and_a_legacy_one_are_no_model_zone(self):
        for zone in ("manual", "header", "", None):
            with self.subTest(zone=zone):
                self.assertFalse(page_numbers.is_model_zone(zone))
