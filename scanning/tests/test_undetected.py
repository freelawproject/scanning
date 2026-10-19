"""The text in no box of review 2 (``undetected``): rows, rule and card.

The compute stores the text cells of every page as one ``TextReading``
row per page, the finding reads those rows against the live detections
with the OCR glue's own measure (``opinion_ocr.page_frame``,
``opinion_ocr.undetected``), and the card offers a ``TEXT_COLUMN`` box
over the block, or a dismissal.
"""

from blackletter.models import Label

from scanning import findings, opinion_ocr, undetected
from scanning.models import (
    ApplyRun,
    CheckName,
    Detection,
    Issue,
    ReviewDismissal,
    TextReading,
)
from scanning.tests.test_findings import (
    make_boundary,
    make_detection,
    make_redaction,
    make_scan,
)
from scanning.tests.test_views import ScanningTestCase

WIDTH = 1700
HEIGHT = 2200

#: A column box the model drew: the left column of the page.
COLUMN = [100.0, 100.0, 900.0, 2000.0]
#: A cell inside it.
INSIDE = [150.0, 200.0, 850.0, 600.0]
#: A cell in the right column, which the model left outside every box.
OUTSIDE = [1000.0, 200.0, 1600.0, 600.0]


def cell(text, bbox=OUTSIDE, kind="Text"):
    return {"bbox": list(bbox), "category": kind, "text": text}


def page(index, *cells, width=WIDTH, height=HEIGHT):
    return {
        "page_index": index,
        "origin_width": width,
        "origin_height": height,
        "cells": list(cells),
    }


def document(*pages):
    return {"pages": list(pages)}


def column(scan, page_index=0, bbox=COLUMN, **fields):
    """Create a live ``TEXT_COLUMN`` row."""
    x0, y0, x1, y1 = bbox
    return make_detection(
        scan,
        Label.TEXT_COLUMN.name,
        page_index=page_index,
        label_id=int(Label.TEXT_COLUMN),
        x0=x0,
        y0=y0,
        x1=x1,
        y1=y1,
        **fields,
    )


class TestTheMeasure(ScanningTestCase):
    """``opinion_ocr.undetected``, the rule the glue and the card share."""

    frame = [{"x0": 36.0, "y0": 36.0, "x1": 324.0, "y1": 720.0}]

    def test_a_page_with_no_frame_is_not_judged(self):
        self.assertFalse(
            opinion_ocr.undetected([400.0, 72.0, 500.0, 200.0], None)
        )

    def test_a_unit_outside_every_box_is_undetected(self):
        self.assertTrue(
            opinion_ocr.undetected([400.0, 72.0, 500.0, 200.0], self.frame)
        )

    def test_a_unit_inside_a_box_is_not(self):
        self.assertFalse(
            opinion_ocr.undetected([54.0, 72.0, 300.0, 200.0], self.frame)
        )

    def test_a_unit_the_frame_covers_by_a_sliver_is_undetected(self):
        # Eighteen points of three hundred and sixty inside the frame.
        self.assertTrue(
            opinion_ocr.undetected([306.0, 72.0, 666.0, 200.0], self.frame)
        )


class TestReadDocument(ScanningTestCase):
    def test_a_cell_is_read_with_its_box_its_kind_and_its_text(self):
        pages = undetected.read_document(
            document(page(2, cell("The   court  held", kind="Footnote")))
        )
        self.assertEqual(
            pages,
            {
                2: (
                    WIDTH,
                    HEIGHT,
                    [
                        {
                            "box": OUTSIDE,
                            "kind": "Footnote",
                            "text": "The court held",
                        }
                    ],
                )
            },
        )

    def test_the_text_is_cut_to_a_line(self):
        pages = undetected.read_document(document(page(0, cell("x" * 200))))
        self.assertEqual(
            len(pages[0][2][0]["text"]), undetected.MAX_TEXT_CHARS
        )

    def test_a_cell_with_no_box_or_no_text_is_skipped(self):
        pages = undetected.read_document(
            document(
                page(
                    0,
                    {"category": "Text", "text": "no box"},
                    cell("   "),
                    cell("flat", bbox=[10.0, 10.0, 10.0, 50.0]),
                )
            )
        )
        self.assertEqual(pages[0][2], [])

    def test_a_page_with_no_render_size_is_skipped(self):
        pages = undetected.read_document(document(page(0, cell("A"), width=0)))
        self.assertEqual(pages, {})

    def test_no_document_reads_nothing(self):
        self.assertEqual(undetected.read_document(None), {})


class TestWriteRows(ScanningTestCase):
    def setUp(self):
        super().setUp()
        self.scan = make_scan()

    def test_it_writes_one_row_per_page_with_its_address(self):
        written = undetected.write_rows(
            self.scan, document(page(1, cell("A"), cell("B"))), None
        )
        self.assertEqual(written, 1)
        row = TextReading.objects.get(scan=self.scan)
        self.assertEqual(row.page_index, 1)
        self.assertEqual(row.source_page, 2)
        self.assertIsNone(row.source_edit_id)
        self.assertEqual((row.img_width, row.img_height), (WIDTH, HEIGHT))
        self.assertEqual([c["text"] for c in row.cells], ["A", "B"])
        self.assertEqual(row.source_fingerprint, self.scan.source_fingerprint)

    def test_a_second_compute_replaces_the_set(self):
        undetected.write_rows(
            self.scan, document(page(0, cell("A")), page(1, cell("B"))), None
        )
        undetected.write_rows(self.scan, document(page(0, cell("A"))), None)
        self.assertEqual(TextReading.objects.filter(scan=self.scan).count(), 1)

    def test_no_document_leaves_the_rows_alone(self):
        undetected.write_rows(self.scan, document(page(0, cell("A"))), None)
        self.assertEqual(undetected.write_rows(self.scan, None, None), 0)
        self.assertEqual(TextReading.objects.filter(scan=self.scan).count(), 1)

    def test_a_new_run_sweeps_the_readings_of_the_old_one(self):
        undetected.write_rows(self.scan, document(page(0, cell("A"))), None)
        run = ApplyRun.objects.create(
            scan=self.scan,
            number=1,
            page_map={
                "pages": [{"source": {"kind": "original", "pdf_page": 1}}]
            },
        )
        undetected.write_rows(self.scan, document(page(0, cell("A"))), run)
        rows = TextReading.objects.filter(scan=self.scan)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().apply_run_id, run.pk)


class TestClusters(ScanningTestCase):
    def test_cells_one_under_the_other_are_one_block(self):
        blocks = undetected.clusters(
            [
                {"box": [1000.0, 650.0, 1600.0, 900.0]},
                {"box": [1000.0, 200.0, 1600.0, 600.0]},
            ]
        )
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["box"], [1000.0, 200.0, 1600.0, 900.0])
        self.assertEqual(len(blocks[0]["cells"]), 2)

    def test_a_wide_gap_starts_another_block(self):
        blocks = undetected.clusters(
            [
                {"box": [1000.0, 200.0, 1600.0, 600.0]},
                {"box": [1000.0, 1500.0, 1600.0, 1800.0]},
            ]
        )
        self.assertEqual(len(blocks), 2)

    def test_the_two_columns_are_two_blocks(self):
        blocks = undetected.clusters(
            [
                {"box": [100.0, 200.0, 800.0, 600.0]},
                {"box": [1000.0, 300.0, 1600.0, 700.0]},
            ]
        )
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0]["box"][0], 100.0)


class TestTheRule(ScanningTestCase):
    """What ``uncovered`` yields, and what it rejects."""

    def setUp(self):
        super().setUp()
        self.scan = make_scan()

    def write(self, *cells, index=0, **kw):
        undetected.write_rows(
            self.scan, document(page(index, *cells, **kw)), None
        )

    def found(self, run=None):
        return list(undetected.uncovered(self.scan, run))

    def test_a_block_outside_every_box_is_a_finding(self):
        column(self.scan)
        self.write(cell("Inside the column", INSIDE), cell("Left outside"))
        found = self.found()
        self.assertEqual(len(found), 1)
        finding = found[0]
        self.assertEqual(finding["check_name"], CheckName.UNDETECTED_TEXT)
        self.assertEqual(finding["target"], Issue.Target.DETECTION)
        self.assertEqual(finding["page_number"], 1)
        self.assertIn("Left outside", finding["message"])
        meta = finding["metadata"]
        self.assertEqual(meta["bbox"], OUTSIDE)
        self.assertEqual(meta["guess"], OUTSIDE)
        self.assertEqual(meta["label"], "TEXT_COLUMN")
        self.assertEqual(meta["label_id"], int(Label.TEXT_COLUMN))
        self.assertEqual(
            (meta["img_width"], meta["img_height"]), (WIDTH, HEIGHT)
        )
        self.assertEqual(meta["source_page"], 1)
        self.assertEqual(meta["cells"], 1)

    def test_a_page_with_no_column_box_is_judged_against_its_boxes(self):
        # The glue keeps every unit of such a page, so the card is no
        # drop there: it says the page has no column box, which the
        # curator who shrank an image box off the text needs to hear.
        make_detection(self.scan, "HEADNOTE", page_index=0)
        self.write(cell("Left outside"))
        found = self.found()
        self.assertEqual(len(found), 1)
        self.assertIn("the page has no column box", found[0]["message"])
        self.assertIn("keeps the block", found[0]["message"])
        self.assertTrue(found[0]["metadata"]["no_column"])

    def test_on_a_page_with_no_column_the_guess_is_the_blocks_column(self):
        # The + adds the column the block sits in: the footnote under
        # it is inside, the running head above it is not, and a cell in
        # the other column is not. The address stays the block. The
        # running head sits in its header box, so it is covered and no
        # card of its own; the footnote is a card of its own, far
        # enough down to be its own block.
        make_detection(
            self.scan,
            "PAGE_HEADER",
            page_index=0,
            x0=1000.0,
            y0=50.0,
            x1=1600.0,
            y1=90.0,
        )
        self.write(
            cell(
                "452",
                [1000.0, 50.0, 1600.0, 90.0],
                kind="Page-header",
            ),
            cell("Left outside"),
            cell(
                "A footnote under it",
                [1050.0, 1800.0, 1550.0, 1900.0],
                kind="Footnote",
            ),
            cell("Other column", [100.0, 200.0, 900.0, 600.0]),
        )
        found = self.found()
        cards = {tuple(f["metadata"]["bbox"]): f for f in found}
        card = cards[tuple(OUTSIDE)]
        self.assertTrue(card["metadata"]["no_column"])
        self.assertEqual(
            card["metadata"]["guess"], [1000.0, 200.0, 1600.0, 1900.0]
        )
        # The other column's card keeps its own column.
        other = cards[(100.0, 200.0, 900.0, 600.0)]
        self.assertEqual(
            other["metadata"]["guess"], [100.0, 200.0, 900.0, 600.0]
        )

    def test_on_a_page_with_a_column_the_guess_is_the_block(self):
        # A page with a column box: the guess is the block alone, and a
        # cell inside the column is covered.
        column(self.scan)
        self.write(
            cell("Left outside"),
            cell(
                "In the column",
                [150.0, 1800.0, 850.0, 1900.0],
                kind="Footnote",
            ),
        )
        found = self.found()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["metadata"]["guess"], OUTSIDE)

    def test_a_box_of_another_label_covers_on_a_page_with_no_column(self):
        make_detection(
            self.scan,
            "IMAGE",
            page_index=0,
            x0=OUTSIDE[0],
            y0=OUTSIDE[1],
            x1=OUTSIDE[2],
            y1=OUTSIDE[3],
        )
        self.write(cell("Under the picture"))
        self.assertEqual(self.found(), [])

    def test_a_page_with_no_box_at_all_is_judged(self):
        self.write(cell("Left outside"))
        found = self.found()
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]["metadata"]["no_column"])
        meta = found[0]["metadata"]
        self.assertEqual(
            (meta["img_width"], meta["img_height"]), (WIDTH, HEIGHT)
        )

    def test_a_page_with_a_column_box_says_the_text_is_lost(self):
        column(self.scan)
        self.write(cell("Left outside"))
        found = self.found()
        self.assertEqual(len(found), 1)
        self.assertIn("leaves out", found[0]["message"])
        self.assertFalse(found[0]["metadata"]["no_column"])

    def test_a_cell_the_box_covers_mostly_is_no_finding(self):
        column(self.scan)
        self.write(cell("Over the edge", [150.0, 200.0, 1100.0, 600.0]))
        self.assertEqual(self.found(), [])

    def test_a_cell_the_box_covers_by_a_sliver_is_a_finding(self):
        column(self.scan)
        self.write(cell("A sliver inside", [850.0, 200.0, 1650.0, 600.0]))
        self.assertEqual(len(self.found()), 1)

    def test_any_live_box_covers(self):
        column(self.scan)
        make_detection(
            self.scan,
            "HEADNOTE",
            page_index=0,
            x0=OUTSIDE[0],
            y0=OUTSIDE[1],
            x1=OUTSIDE[2],
            y1=OUTSIDE[3],
        )
        self.write(cell("Under a headnote box"))
        self.assertEqual(self.found(), [])

    def test_a_visible_redaction_is_cover(self):
        # The verdict reads the redactions first: a unit a redaction
        # covers is excluded as a redaction, never dropped as
        # undetected, so a block a curator blacked out makes no card.
        column(self.scan)
        make_redaction(
            self.scan,
            page_index=0,
            origin="human",
            kind="add",
            rect_type="manual",
            x0=350.0,
            y0=60.0,
            x1=590.0,
            y1=230.0,
        )
        self.write(cell("Blacked out by hand"))
        self.assertEqual(self.found(), [])

    def test_a_redaction_on_another_page_is_no_cover(self):
        column(self.scan)
        make_redaction(
            self.scan, page_index=1, x0=350.0, y0=60.0, x1=590.0, y1=230.0
        )
        self.write(cell("Left outside"))
        self.assertEqual(len(self.found()), 1)

    def test_a_row_with_no_render_size_still_covers(self):
        # The glue's frame is every live row of the run, a row with no
        # render size mapped at the render density.
        column(self.scan)
        make_detection(
            self.scan,
            "HEADNOTE",
            page_index=0,
            x0=OUTSIDE[0],
            y0=OUTSIDE[1],
            x1=OUTSIDE[2],
            y1=OUTSIDE[3],
            img_width=0,
            img_height=0,
        )
        self.write(cell("Under a box with no render"))
        self.assertEqual(self.found(), [])

    def test_a_deactivated_box_does_not_cover(self):
        column(self.scan)
        column(self.scan, bbox=OUTSIDE, active=False)
        self.write(cell("Left outside"))
        self.assertEqual(len(self.found()), 1)

    def test_no_kind_is_skipped_the_boxes_decide(self):
        # The reader's category never hides a cell: a footnote line the
        # model's footnote box stopped short of is a drop, whatever the
        # reader called it (page 221 of 452 P.3d, a footnote labelled a
        # page foot). The four cells share one place, so one card.
        column(self.scan)
        self.write(
            cell("452 P.3d", kind="Page-header"),
            cell("Page 7", kind="Page-footer"),
            cell("a picture", kind="Picture"),
            cell("1. A footnote the box stopped short of", kind="Footnote"),
        )
        found = self.found()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["metadata"]["cells"], 4)

    def test_a_footnote_inside_the_footnote_box_is_covered(self):
        column(self.scan)
        make_detection(
            self.scan,
            "FOOTNOTES",
            page_index=0,
            x0=OUTSIDE[0],
            y0=OUTSIDE[1],
            x1=OUTSIDE[2],
            y1=OUTSIDE[3],
        )
        self.write(cell("1. A footnote in its box", kind="Footnote"))
        self.assertEqual(self.found(), [])

    def test_a_box_past_the_page_edge_is_judged_as_the_page_holds_it(self):
        # Page 100 of 452 P.3d: a footnote cell ten pages wide, inside
        # the footnote box once held to the page, so no card.
        column(self.scan)
        make_detection(
            self.scan,
            "FOOTNOTES",
            page_index=0,
            x0=300.0,
            y0=1900.0,
            x1=1500.0,
            y1=2000.0,
        )
        self.write(
            cell("839 (Colo. App. 2007).", [900.0, 1920.0, 11387.0, 1990.0])
        )
        self.assertEqual(self.found(), [])

    def test_a_card_never_names_a_box_past_the_page(self):
        column(self.scan)
        self.write(cell("Past the edge", [1000.0, 200.0, 11387.0, 600.0]))
        found = self.found()
        self.assertEqual(len(found), 1)
        self.assertEqual(
            found[0]["metadata"]["bbox"], [1000.0, 200.0, 1700.0, 600.0]
        )

    def test_a_box_wholly_past_the_page_is_no_cell(self):
        # No cell is ever past the page: with nothing of it on the
        # page there is no box to judge, so no card, and the glue
        # leaves the unit unjudged.
        column(self.scan)
        self.write(cell("Off the page", [2000.0, 200.0, 2600.0, 600.0]))
        self.assertEqual(self.found(), [])
        self.assertIsNone(
            opinion_ocr.clamp_box(
                [2000.0, 200.0, 2600.0, 600.0], WIDTH, HEIGHT
            )
        )

    def test_a_box_past_the_page_is_cut_at_the_box_that_holds_it(self):
        # A cell that starts at the right of its column and runs off
        # the page: cut at the column's edge it is wholly inside, so
        # no card; cut at the page edge it would be a card.
        column(self.scan, bbox=[900.0, 100.0, 1600.0, 2000.0])
        self.write(cell("Near the edge", [1400.0, 200.0, 11387.0, 600.0]))
        self.assertEqual(self.found(), [])

    def test_every_cell_outside_the_boxes_is_a_card(self):
        # No word test and no kind test: a short mark, a bare number and
        # a footnote of pairs are cells like any other, and the cover
        # tests alone decide. The one in the column box is covered.
        column(self.scan)
        self.write(
            cell("— ."),
            cell("12", [1000.0, 800.0, 1600.0, 900.0]),
            cell("17", [150.0, 300.0, 250.0, 340.0]),
        )
        self.assertEqual(
            sorted(f["metadata"]["bbox"][1] for f in self.found()),
            [200.0, 800.0],
        )

    def test_a_short_footnote_is_text(self):
        # "53. Id. at 16-18." (page 961 of 433 F. Supp. 3d) has no run
        # of three letters or digits, and is a footnote the footnote
        # box stopped short of.
        column(self.scan)
        self.write(cell("53. *Id.* at 16-18."))
        self.assertEqual(len(self.found()), 1)

    def test_the_cells_of_a_block_make_one_card(self):
        column(self.scan)
        self.write(
            cell("First", [1000.0, 200.0, 1600.0, 600.0]),
            cell("Second", [1000.0, 650.0, 1600.0, 900.0]),
            cell("Far below", [1000.0, 1500.0, 1600.0, 1800.0]),
        )
        found = self.found()
        self.assertEqual(len(found), 2)
        self.assertEqual(
            found[0]["metadata"]["bbox"], [1000.0, 200.0, 1600.0, 900.0]
        )
        self.assertEqual(found[0]["metadata"]["cells"], 2)
        self.assertIn("2 blocks of text", found[0]["message"])
        self.assertEqual(found[1]["metadata"]["cells"], 1)

    def test_the_box_is_scaled_to_the_detections_render(self):
        column(self.scan)
        self.write(
            cell("Half size", [500.0, 100.0, 800.0, 300.0]),
            width=850,
            height=1100,
        )
        found = self.found()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["metadata"]["bbox"], OUTSIDE)
        self.assertEqual(found[0]["metadata"]["img_width"], WIDTH)

    def test_a_row_of_another_run_does_not_cover(self):
        # The glue reads the run's space alone: a human row the
        # relocation could not place keeps its old index and its old
        # run, and is a stale card, not cover.
        column(self.scan)
        run = ApplyRun.objects.create(scan=self.scan, number=1, page_map={})
        column(
            self.scan,
            bbox=OUTSIDE,
            model_name=Detection.ModelName.MANUAL,
            confidence=1.0,
            apply_run=run,
        )
        self.write(cell("Left outside"))
        self.assertEqual(len(self.found()), 1)

    def test_a_reading_of_another_run_is_not_read(self):
        column(self.scan)
        self.write(cell("Left outside"))
        run = ApplyRun.objects.create(scan=self.scan, number=1, page_map={})
        self.assertEqual(self.found(run), [])

    def test_no_reading_gives_no_finding(self):
        column(self.scan)
        self.assertEqual(self.found(), [])


class TestTheCard(ScanningTestCase):
    """The finding through ``findings.rebuild``, and its dismissal."""

    def setUp(self):
        super().setUp()
        self.scan = make_scan()
        # The rebuild writes no measured finding without a computed
        # boundary (the findings docstring), so the volume has one.
        caption = make_detection(self.scan, "CASE_CAPTION", page_index=0)
        key = make_detection(self.scan, "KEY_ICON", page_index=1)
        make_boundary(self.scan, caption, key)
        column(self.scan)
        undetected.write_rows(
            self.scan,
            document(page(0, cell("Inside", INSIDE), cell("Left outside"))),
            None,
        )

    def cards(self):
        return Issue.objects.filter(
            scan=self.scan, check_name=CheckName.UNDETECTED_TEXT
        )

    def test_the_rebuild_writes_the_card(self):
        findings.rebuild(self.scan, run=None)
        card = self.cards().get()
        self.assertEqual(card.severity, Issue.Severity.WARNING)
        self.assertEqual(card.metadata["guess"], OUTSIDE)

    def test_drawing_the_column_takes_the_card_away(self):
        findings.rebuild(self.scan, run=None)
        column(
            self.scan,
            bbox=[980.0, 180.0, 1620.0, 620.0],
            model_name=Detection.ModelName.MANUAL,
            confidence=1.0,
        )
        findings.rebuild(self.scan, run=None)
        self.assertEqual(self.cards().count(), 0)

    def test_a_dismissal_lands_on_the_card_again(self):
        findings.rebuild(self.scan, run=None)
        findings.dismiss(self.scan, self.cards().get(), self.make_user())
        findings.rebuild(self.scan, run=None)
        self.assertIsNotNone(self.cards().get().dismissal_id)
        self.assertEqual(ReviewDismissal.objects.count(), 1)


#: A line of text in the reader's render: x 108-540 pt, y 360-378 pt.
LINE = [300.0, 1000.0, 1500.0, 1050.0]


class TestTheCutLine(ScanningTestCase):
    """``cut_lines``: a black redaction that covers part of a line."""

    def setUp(self):
        super().setUp()
        self.scan = make_scan()

    def write(self, *cells):
        undetected.write_rows(self.scan, document(page(0, *cells)), None)

    def found(self):
        return list(undetected.cut_lines(self.scan, None))

    def test_a_box_that_stops_short_of_a_line_is_a_card(self):
        """The editor's note of 218 A.3d: the box ends before the line."""
        box = make_redaction(
            self.scan,
            rect_type="EDITORIAL",
            x0=100.0,
            y0=355.0,
            x1=470.0,
            y1=385.0,
        )
        self.write(cell("the words of the line", LINE))

        [finding] = self.found()

        self.assertEqual(finding["check_name"], CheckName.REDACTION_CUTS_TEXT)
        self.assertEqual(finding["target"], Issue.Target.REDACTION)
        self.assertEqual(finding["page_number"], 1)
        meta = finding["metadata"]
        self.assertEqual(meta["redaction_id"], box.pk)
        self.assertEqual(meta["label"], "EDITORIAL")
        self.assertEqual(meta["bbox"], LINE)
        self.assertEqual(meta["source_page"], 1)
        # The box grown over the line, in points: what "Cover the line"
        # moves it to.
        self.assertEqual(meta["cover"], [100.0, 355.0, 540.0, 385.0])
        self.assertTrue(
            opinion_ocr.EXCLUDE_SHARE <= meta["share"] < opinion_ocr.FULL_SHARE
        )
        self.assertIn("An editor's note box covers", finding["message"])

    def test_the_message_quotes_no_word_of_the_line(self):
        """The line is partly under the box: its words may be redacted."""
        make_redaction(self.scan, x0=100.0, y0=355.0, x1=470.0, y1=385.0)
        self.write(cell("Secret words under the box", LINE))

        [finding] = self.found()

        self.assertNotIn("Secret", finding["message"])

    def test_a_box_over_the_whole_line_is_no_card(self):
        make_redaction(self.scan, x0=100.0, y0=355.0, x1=560.0, y1=385.0)
        self.write(cell("the words of the line", LINE))

        self.assertEqual(self.found(), [])

    def test_a_box_beside_the_line_is_no_card(self):
        make_redaction(self.scan, x0=100.0, y0=500.0, x1=560.0, y1=530.0)
        self.write(cell("the words of the line", LINE))

        self.assertEqual(self.found(), [])

    def test_a_glyphs_box_and_a_white_box_make_no_card(self):
        """A bracket, a key and a divider cover a symbol; a white box is
        a margin or the running head."""
        for rect_type in ("HEADNOTE_BRACKET", "KEY_ICON", "DIVIDER"):
            make_redaction(
                self.scan,
                rect_type=rect_type,
                x0=100.0,
                y0=355.0,
                x1=300.0,
                y1=385.0,
            )
        make_redaction(
            self.scan,
            rect_type="margin",
            fill="white",
            x0=100.0,
            y0=355.0,
            x1=300.0,
            y1=385.0,
        )
        self.write(cell("the words of the line", LINE))

        self.assertEqual(self.found(), [])

    def test_a_speck_is_no_card(self):
        make_redaction(self.scan, x0=100.0, y0=355.0, x1=470.0, y1=385.0)
        self.write(cell("a.", LINE))

        self.assertEqual(self.found(), [])

    def test_the_running_heads_line_is_no_card(self):
        """Review 3 takes the head whole and the whiteout covers it, so
        a headnote box drawn up into it hides no text."""
        head = [300.0, 50.0, 1500.0, 90.0]
        make_detection(
            self.scan,
            "PAGE_HEADER",
            x0=head[0],
            y0=head[1],
            x1=head[2],
            y1=head[3],
        )
        make_redaction(self.scan, x0=100.0, y0=10.0, x1=300.0, y1=40.0)
        self.write(cell("THE RUNNING HEAD OF THE PAGE", head))

        self.assertEqual(self.found(), [])

    def test_the_rebuild_writes_the_card_and_a_move_over_the_line_ends_it(
        self,
    ):
        make_boundary(
            self.scan,
            make_detection(self.scan, "CASE_CAPTION", page_index=1),
            make_detection(self.scan, "KEY_ICON", page_index=1),
        )
        box = make_redaction(self.scan, x0=100.0, y0=355.0, x1=470.0, y1=385.0)
        self.write(cell("the words of the line", LINE))

        findings.rebuild(self.scan)
        issue = Issue.objects.get(
            scan=self.scan, check_name=CheckName.REDACTION_CUTS_TEXT
        )
        cover = issue.metadata["cover"]
        box.x1 = cover[2]
        box.save()
        findings.rebuild(self.scan)

        self.assertFalse(
            Issue.objects.filter(
                scan=self.scan, check_name=CheckName.REDACTION_CUTS_TEXT
            ).exists()
        )
