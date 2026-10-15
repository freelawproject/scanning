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

    def test_the_kinds_that_make_no_card(self):
        column(self.scan)
        self.write(
            cell("452 P.3d", kind="Page-header"),
            cell("Page 7", kind="Page-footer"),
            cell("a picture", kind="Picture"),
            cell("1. A footnote the box stopped short of", kind="Footnote"),
        )
        self.assertEqual(self.found(), [])

    def test_a_stray_mark_is_no_card(self):
        column(self.scan)
        self.write(cell("— ."), cell("a b"), cell("12"))
        self.assertEqual(self.found(), [])

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
