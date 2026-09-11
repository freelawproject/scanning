"""Tests for the dots.mocr text survey of review 2 (issue #303).

The module ``text_findings`` measures what the dots.mocr cells would
say about the detection and redaction rows, and writes nothing. Tested
here: the projection that keeps the text, the three probes, the totals,
and the command that reports them.

The render is 1700 by 2200 pixels throughout, which at
``dots_mocr.DPI`` is a 612 by 792 point page: US Letter. So a box in
points is its pixel box times 0.36, and a reader of these tests can
check the arithmetic by hand.
"""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError

from scanning import text_findings
from scanning.models import Detection, Redaction
from scanning.tests.test_findings import (
    make_detection,
    make_redaction,
    make_scan,
)
from scanning.tests.test_views import ScanningTestCase

#: The render both spaces use, in pixels.
WIDTH, HEIGHT = 1700, 2200

#: One key icon glyph, as dots writes it.
ARROW = "⇨"


def cell(x0, y0, x1, y1, category="Text", text=""):
    """Build one layout cell of a glued page.

    :param x0: Left, in render pixels.
    :param y0: Top, in render pixels.
    :param x1: Right, in render pixels.
    :param y1: Bottom, in render pixels.
    :param category: The cell's category.
    :param text: The cell's text.
    :returns: The cell dict.
    """
    return {
        "bbox": [x0, y0, x1, y1],
        "category": category,
        "text": text,
    }


def document(*pages):
    """Build a glued OCR document from page dicts.

    :param pages: One dict per page, each with ``cells`` and optionally
        ``render_fallback``.
    :returns: The document.
    """
    return {
        "pages": [
            {
                "page_index": index,
                "origin_width": WIDTH,
                "origin_height": HEIGHT,
                **page,
            }
            for index, page in enumerate(pages)
        ]
    }


class TestPageRegions(ScanningTestCase):
    """``page_regions`` keeps the text the fit throws away."""

    def test_keeps_the_category_and_the_text(self):
        """A cell arrives with its category and its text."""
        regions = text_findings.page_regions(
            document({"cells": [cell(10, 20, 30, 40, "Page-header", "677")]})
        )
        self.assertEqual(list(regions), [0])
        region = regions[0].regions[0]
        self.assertEqual(region.box, (10.0, 20.0, 30.0, 40.0))
        self.assertEqual(region.category, "Page-header")
        self.assertEqual(region.text, "677")

    def test_a_missing_category_or_text_is_the_empty_string(self):
        """A cell with neither key reads as empty, never None."""
        regions = text_findings.page_regions(
            document({"cells": [{"bbox": [1, 2, 3, 4]}]})
        )
        region = regions[0].regions[0]
        self.assertEqual(region.category, "")
        self.assertEqual(region.text, "")

    def test_a_page_with_no_usable_cell_gives_no_entry(self):
        """A failed page, a filtered page and an insert are all skipped."""
        regions = text_findings.page_regions(
            document(
                {"cells": []},
                {"cells": [{"bbox": [5, 5, 1, 1]}]},
                {"cells": [cell(1, 2, 3, 4)]},
            )
        )
        self.assertEqual(list(regions), [2])

    def test_the_render_fallback_flag_travels(self):
        """The redaction probe needs it, so it must survive the read."""
        regions = text_findings.page_regions(
            document({"cells": [cell(1, 2, 3, 4)], "render_fallback": True})
        )
        self.assertTrue(regions[0].fallback)

    def test_a_page_with_no_render_size_is_skipped(self):
        """Without the render there is nothing to divide by."""
        self.assertEqual(
            text_findings.page_regions(
                {
                    "pages": [
                        {
                            "page_index": 0,
                            "origin_width": 0,
                            "origin_height": HEIGHT,
                            "cells": [cell(1, 2, 3, 4)],
                        }
                    ]
                }
            ),
            {},
        )

    def test_nothing_reads_as_nothing(self):
        """A document that did not load gives an empty map."""
        self.assertEqual(text_findings.page_regions(None), {})
        self.assertEqual(text_findings.page_regions({"pages": None}), {})


class TestBracketPattern(ScanningTestCase):
    """The bracketed number only matches at the start of a line."""

    def test_a_headnote_number_matches(self):
        """The shapes a reporter prints."""
        for text in ("[1] Criminal Law", "[2, 3] Evidence", "[4-6] Damages"):
            with self.subTest(text=text):
                self.assertTrue(text_findings.BRACKET_RE.match(text))

    def test_a_key_icon_glyph_may_stand_before_it(self):
        """The glyph of the same headnote comes first on the page.

        A pattern anchored hard at the line start missed every headnote
        that carries a key icon, which are the ones this survey most
        wants to find.
        """
        for text in (f"{ARROW} [1] Criminal Law", "▶[2] Evidence"):
            with self.subTest(text=text):
                self.assertTrue(text_findings.BRACKET_RE.match(text))

    def test_a_bracketed_year_in_a_citation_does_not(self):
        """A four-digit number is not a headnote number."""
        for text in ("[1999] AC 12", "see [1999] AC 12"):
            with self.subTest(text=text):
                self.assertIsNone(text_findings.BRACKET_RE.match(text))

    def test_a_bracket_after_a_word_does_not(self):
        """Only a glyph may stand before it, never a letter or a digit."""
        for text in ("the court [1] said", "12 [1] of the act"):
            with self.subTest(text=text):
                self.assertIsNone(text_findings.BRACKET_RE.match(text))


class TestGlyphProbe(ScanningTestCase):
    """The arrow glyph, against the key icon rows."""

    def setUp(self):
        """Build a scan with one page of cells."""
        super().setUp()
        self.scan = make_scan()
        self.regions = text_findings.page_regions(
            document(
                {
                    "cells": [
                        cell(100, 100, 800, 300, "Text", f"{ARROW} 1 Damages")
                    ]
                }
            )
        )

    def survey(self):
        """Run the probes over the scan.

        :returns: The volume's totals.
        """
        return text_findings.survey_scan(self.scan, None, self.regions)

    def test_a_key_icon_in_the_cell_covers_the_glyph(self):
        """YOLO saw what dots saw, so the cell raises nothing."""
        make_detection(
            self.scan,
            "KEY_ICON",
            x0=120,
            y0=120,
            x1=170,
            y1=170,
            img_width=WIDTH,
            img_height=HEIGHT,
        )
        counts = self.survey().glyphs[ARROW]
        self.assertEqual(
            (counts.cells, counts.covered, counts.uncovered), (1, 1, 0)
        )

    def test_a_key_icon_elsewhere_on_the_page_does_not(self):
        """A row far from the cell is another key icon, not this one."""
        make_detection(
            self.scan,
            "KEY_ICON",
            x0=1500,
            y0=2000,
            x1=1550,
            y1=2050,
            img_width=WIDTH,
            img_height=HEIGHT,
        )
        counts = self.survey().glyphs[ARROW]
        self.assertEqual(
            (counts.cells, counts.covered, counts.uncovered), (1, 0, 1)
        )
        self.assertEqual(counts.pages, {(self.scan.pk, 0)})

    def test_no_row_at_all_leaves_the_glyph_uncovered(self):
        """The candidate finding: a key icon YOLO missed."""
        counts = self.survey().glyphs[ARROW]
        self.assertEqual(counts.uncovered, 1)
        self.assertEqual(counts.share, 1.0)

    def test_a_row_of_another_label_does_not_cover_it(self):
        """Only a ``KEY_ICON`` answers this probe."""
        make_detection(
            self.scan,
            "HEADNOTE",
            x0=120,
            y0=120,
            x1=170,
            y1=170,
            img_width=WIDTH,
            img_height=HEIGHT,
        )
        self.assertEqual(self.survey().glyphs[ARROW].uncovered, 1)

    def test_a_deactivated_row_does_not_cover_it(self):
        """A row a curator took out is not a witness."""
        make_detection(
            self.scan,
            "KEY_ICON",
            x0=120,
            y0=120,
            x1=170,
            y1=170,
            img_width=WIDTH,
            img_height=HEIGHT,
            active=False,
        )
        self.assertEqual(self.survey().glyphs[ARROW].uncovered, 1)

    def test_a_row_with_no_render_size_is_not_a_witness(self):
        """Without ``img_width`` its centre cannot be placed."""
        make_detection(
            self.scan,
            "KEY_ICON",
            x0=120,
            y0=120,
            x1=170,
            y1=170,
            img_width=0,
            img_height=0,
        )
        self.assertEqual(self.survey().glyphs[ARROW].uncovered, 1)

    def test_every_glyph_is_counted_on_its_own(self):
        """The survey says which character the model writes."""
        self.regions = text_findings.page_regions(
            document(
                {
                    "cells": [
                        cell(100, 100, 800, 300, "Text", f"{ARROW}{ARROW}"),
                        cell(900, 100, 1600, 300, "Text", "→ see"),
                    ]
                }
            )
        )
        glyphs = self.survey().glyphs
        self.assertEqual(glyphs[ARROW].hits, 2)
        self.assertEqual(glyphs[ARROW].cells, 1)
        self.assertEqual(glyphs["→"].hits, 1)

    def test_a_letter_is_not_a_glyph(self):
        """Only the arrow and geometric blocks are counted."""
        self.regions = text_findings.page_regions(
            document(
                {"cells": [cell(100, 100, 800, 300, "Text", "1 Damages")]}
            )
        )
        self.assertEqual(self.survey().glyphs, {})


class TestBracketProbe(ScanningTestCase):
    """The bracketed number, against the headnote rows."""

    def setUp(self):
        """Build a scan with one headnote cell of two headnotes."""
        super().setUp()
        self.scan = make_scan()
        self.regions = text_findings.page_regions(
            document(
                {
                    "cells": [
                        cell(
                            100,
                            100,
                            800,
                            300,
                            "Text",
                            "[1] Criminal Law\nand more\n[2] Evidence",
                        )
                    ]
                }
            )
        )

    def survey(self):
        """Run the probes over the scan.

        :returns: The volume's totals.
        """
        return text_findings.survey_scan(self.scan, None, self.regions)

    def test_the_hits_count_the_headnotes_and_the_cells_the_cell(self):
        """One cell can hold several headnotes."""
        counts = self.survey().brackets
        self.assertEqual((counts.cells, counts.hits), (1, 2))

    def test_a_headnote_bracket_row_covers_the_cell(self):
        """Either label answers this probe."""
        make_detection(
            self.scan,
            "HEADNOTE_BRACKET",
            x0=110,
            y0=110,
            x1=130,
            y1=130,
            img_width=WIDTH,
            img_height=HEIGHT,
        )
        self.assertEqual(self.survey().brackets.covered, 1)

    def test_a_headnote_row_covers_the_cell(self):
        """The second of the two labels."""
        make_detection(
            self.scan,
            "HEADNOTE",
            x0=110,
            y0=110,
            x1=130,
            y1=130,
            img_width=WIDTH,
            img_height=HEIGHT,
        )
        self.assertEqual(self.survey().brackets.covered, 1)

    def test_no_row_leaves_it_uncovered(self):
        """The candidate finding: a headnote YOLO missed."""
        counts = self.survey().brackets
        self.assertEqual(counts.uncovered, 1)
        self.assertEqual(counts.pages, {(self.scan.pk, 0)})


class TestHeaderProbe(ScanningTestCase):
    """The running head, against the white redaction rows."""

    def setUp(self):
        """Build a scan with one running-head cell."""
        super().setUp()
        self.scan = make_scan()
        self.regions = text_findings.page_regions(
            document(
                {
                    "cells": [
                        cell(
                            100,
                            50,
                            1600,
                            120,
                            "Page-header",
                            "677 ATLANTIC REPORTER, 2d SERIES",
                        ),
                        cell(100, 300, 800, 900, "Text", "the opinion"),
                    ]
                }
            )
        )

    def survey(self):
        """Run the probes over the scan.

        :returns: The volume's totals.
        """
        return text_findings.survey_scan(self.scan, None, self.regions)

    def test_only_the_running_head_is_read(self):
        """A ``Text`` cell is not this probe's business."""
        self.assertEqual(self.survey().headers.cells, 1)

    def test_a_white_row_over_it_covers_it(self):
        """The cell in points is (36, 18) to (576, 43.2)."""
        make_redaction(
            self.scan,
            rect_type="PAGE_HEADER",
            fill=Redaction.Fill.WHITE,
            x0=30.0,
            y0=15.0,
            x1=580.0,
            y1=50.0,
        )
        self.assertEqual(self.survey().headers.covered, 1)

    def test_a_white_row_that_barely_meets_it_does_not(self):
        """Half the cell is the bar, so a corner is not cover."""
        make_redaction(
            self.scan,
            rect_type="PAGE_HEADER",
            fill=Redaction.Fill.WHITE,
            x0=30.0,
            y0=15.0,
            x1=100.0,
            y1=50.0,
        )
        self.assertEqual(self.survey().headers.uncovered, 1)

    def test_a_black_row_does_not_cover_it(self):
        """A running head is painted white; a black box is another thing."""
        make_redaction(
            self.scan,
            x0=30.0,
            y0=15.0,
            x1=580.0,
            y1=50.0,
        )
        self.assertEqual(self.survey().headers.uncovered, 1)

    def test_a_dismissed_row_does_not_cover_it(self):
        """A box a curator took out paints nothing."""
        row = make_redaction(
            self.scan,
            rect_type="PAGE_HEADER",
            fill=Redaction.Fill.WHITE,
            x0=30.0,
            y0=15.0,
            x1=580.0,
            y1=50.0,
        )
        dismissal = make_redaction(
            self.scan,
            origin=Redaction.Origin.HUMAN,
            kind=Redaction.Kind.DISMISS,
            rect_type="PAGE_HEADER",
            fill=Redaction.Fill.WHITE,
            x0=None,
            y0=None,
            x1=None,
            y1=None,
        )
        Redaction.objects.filter(pk=row.pk).update(decision=dismissal)
        self.assertEqual(self.survey().headers.uncovered, 1)

    def test_a_re_rendered_page_is_skipped(self):
        """Its page size cannot be derived from the render."""
        self.regions = text_findings.page_regions(
            document(
                {
                    "cells": [cell(100, 50, 1600, 120, "Page-header", "677")],
                    "render_fallback": True,
                }
            )
        )
        self.assertEqual(self.survey().headers.cells, 0)


class TestSurveyTotals(ScanningTestCase):
    """The census and the folding of one volume into a corpus pass."""

    def test_every_category_is_counted(self):
        """No code may assume the vocabulary, so the survey reports it."""
        scan = make_scan()
        regions = text_findings.page_regions(
            document(
                {
                    "cells": [
                        cell(1, 2, 3, 4, "Page-header", ""),
                        cell(5, 6, 7, 8, "Text", ""),
                        cell(9, 10, 11, 12, "Text", ""),
                        cell(13, 14, 15, 16, "", ""),
                    ]
                }
            )
        )
        survey = text_findings.survey_scan(scan, None, regions)
        self.assertEqual(
            dict(survey.categories),
            {"Page-header": 1, "Text": 2, "(none)": 1},
        )
        self.assertEqual((survey.volumes, survey.pages), (1, 1))

    def test_a_volume_folds_into_the_corpus_totals(self):
        """Two volumes add up, per glyph and per probe."""
        total = text_findings.Survey()
        for _ in range(2):
            one = text_findings.Survey(volumes=1, pages=3)
            one.categories["Text"] += 4
            one.glyph(ARROW).count(False, (1, 0), 2)
            one.brackets.count(True, (1, 0), 1)
            total.add(one)
        self.assertEqual((total.volumes, total.pages), (2, 6))
        self.assertEqual(total.categories["Text"], 8)
        self.assertEqual(total.glyphs[ARROW].hits, 4)
        self.assertEqual(total.glyphs[ARROW].uncovered, 2)
        self.assertEqual(total.brackets.covered, 2)

    def test_the_share_of_an_empty_probe_is_zero(self):
        """No division by zero in the report."""
        self.assertEqual(text_findings.ProbeCounts().share, 0.0)


class TestSurveyCommand(ScanningTestCase):
    """The command reports the totals and writes nothing."""

    def setUp(self):
        """Build a computed volume with one uncovered glyph."""
        super().setUp()
        self.scan = make_scan()
        make_redaction(self.scan)
        self.regions = text_findings.page_regions(
            document(
                {
                    "cells": [
                        cell(
                            100,
                            100,
                            800,
                            300,
                            "Text",
                            f"{ARROW} [1] Criminal Law",
                        ),
                        cell(100, 50, 1600, 120, "Page-header", "677 A.3d"),
                    ]
                }
            )
        )

    def run_command(self, *args):
        """Run the command with the cells stubbed in.

        :param args: Extra CLI arguments.
        :returns: What it printed.
        """
        out = StringIO()
        with (
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch(
                "scanning.text_findings.load_regions",
                return_value=self.regions,
            ),
        ):
            call_command("survey_text_findings", *args, stdout=out, stderr=out)
        return out.getvalue()

    def test_it_reports_each_probe(self):
        """The three probes and the category census reach the report."""
        output = self.run_command()
        self.assertIn("1 volume(s), 1 page(s) with cells", output)
        self.assertIn("U+21E8", output)
        self.assertIn("named in #303", output)
        self.assertIn("the bracketed numbers:", output)
        self.assertIn("the running heads:", output)
        self.assertIn("Page-header: 1", output)
        self.assertIn("nothing written", output)

    def test_it_names_the_example_pages(self):
        """A curator needs an address to open."""
        self.assertIn(f"{self.scan.pk}/1", self.run_command())

    def test_it_writes_nothing(self):
        """A survey that changed a row would not be a survey."""
        before = (
            Detection.objects.count(),
            Redaction.objects.count(),
            self.scan.issues.count(),
        )
        self.run_command()
        self.assertEqual(
            (
                Detection.objects.count(),
                Redaction.objects.count(),
                self.scan.issues.count(),
            ),
            before,
        )

    def test_a_volume_with_no_computed_row_is_not_read(self):
        """The compute has not reached it, so there is nothing to measure."""
        other = make_scan()
        output = self.run_command(str(other.pk))
        self.assertIn("no computed redaction row", output)

    def test_the_limit_caps_the_volumes_read(self):
        """A first look must not read the whole corpus."""
        second = make_scan()
        make_redaction(second)
        self.assertIn("1 volume(s) surveyed", self.run_command("--limit", "1"))

    def test_a_volume_with_no_cell_is_skipped_with_its_reason(self):
        """A document that did not load costs one line, not the pass."""
        self.regions = {}
        output = self.run_command(str(self.scan.pk))
        self.assertIn("the OCR volume holds no cell", output)
        self.assertIn("0 volume(s) surveyed", output)

    def test_it_refuses_without_s3(self):
        """The cells live in the bucket; there is nothing to read."""
        with patch("scanning.s3_sync.s3_active", return_value=False):
            with self.assertRaises(CommandError):
                call_command("survey_text_findings", stdout=StringIO())
