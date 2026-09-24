"""Tests for the headnote brackets the model missed (issue #328).

``brackets`` has two halves. :func:`brackets.write_rows` runs in the
compute and stores one ``BracketReading`` row per bracket the reader
found; :func:`brackets.missing` runs in ``findings.rebuild``, reads
rows only, and writes a finding for a bracket that has no box and whose
number its opinion is missing. Tested here: the token, the reading of a
document, the rows, the rule, the card through the rebuild, and the
backfill command.
"""

from io import StringIO

from django.core.management import call_command
from django.urls import reverse

from scanning import brackets, findings
from scanning.models import (
    ApplyRun,
    BracketReading,
    CheckName,
    Detection,
    Issue,
    ReviewDismissal,
    Status,
)
from scanning.tests.test_findings import (
    make_boundary,
    make_detection,
    make_scan,
)
from scanning.tests.test_views import ScanningTestCase

WIDTH = 1700
HEIGHT = 2200

#: A cell that sits below the default caption anchor of
#: ``make_boundary``, so every reading of a test falls inside the
#: opinion.
CELL = [200.0, 400.0, 800.0, 900.0]


def page(index, *cells, width=WIDTH, height=HEIGHT):
    """Build one page of a glued OCR document.

    :param index: The 0-based page index.
    :param cells: ``(text, bbox)`` pairs, or plain texts for
        :data:`CELL`.
    :param width: The render width.
    :param height: The render height.
    :returns: The page dict.
    """
    out = []
    for cell in cells:
        text, bbox = cell if isinstance(cell, tuple) else (cell, CELL)
        out.append({"bbox": list(bbox), "category": "Text", "text": text})
    return {
        "page_index": index,
        "origin_width": width,
        "origin_height": height,
        "cells": out,
    }


def document(*pages):
    """Build a glued OCR document from :func:`page` results."""
    return {"pages": list(pages)}


def box_over(scan, page_index=0, bbox=None, **fields):
    """Create a live ``HEADNOTE_BRACKET`` row inside :data:`CELL`."""
    x0, y0, x1, y1 = bbox or (210.0, 410.0, 260.0, 450.0)
    return make_detection(
        scan,
        brackets.BRACKET_LABEL,
        page_index=page_index,
        x0=x0,
        y0=y0,
        x1=x1,
        y1=y1,
        **fields,
    )


class TestTheToken(ScanningTestCase):
    """What the reader writes, and what counts as a bracket."""

    def numbers(self, text):
        """The numbers a text gives, or None when it is no bracket."""
        match = brackets.TOKEN.match(text)
        return (brackets.expand(match) or None) if match else None

    def test_one_number(self):
        self.assertEqual(self.numbers("[7] The court"), (7,))

    def test_a_pair(self):
        self.assertEqual(self.numbers("[1, 2] Double jeopardy"), (1, 2))

    def test_a_range_spans_every_number(self):
        self.assertEqual(self.numbers("[16-19] And when"), (16, 17, 18, 19))

    def test_an_en_dash_is_a_range(self):
        self.assertEqual(self.numbers("[1–5] Equitable"), (1, 2, 3, 4, 5))

    def test_a_printed_page_number_is_not_a_headnote(self):
        self.assertIsNone(self.numbers("[120] Providing access"))

    def test_zero_is_not_a_headnote(self):
        self.assertIsNone(self.numbers("[0] Nothing"))

    def test_a_backward_range_is_refused(self):
        self.assertIsNone(self.numbers("[9-2] Nothing"))

    def test_a_bracket_inside_a_word_is_not_a_token(self):
        self.assertIsNone(self.numbers("the defendant[s] argued"))


class TestReadDocument(ScanningTestCase):
    """The readings of one glued OCR document."""

    def test_a_bracket_at_the_start_of_a_cell_is_read(self):
        readings = brackets.read_document(document(page(0, "[7] The court")))
        self.assertEqual(len(readings[0]), 1)
        self.assertEqual(readings[0][0].numbers, (7,))
        self.assertEqual(readings[0][0].raw, "[7]")

    def test_a_bracket_inside_a_paragraph_is_not_read(self):
        # The star-pagination mark of a regional reporter, which
        # dots.mocr writes as a bracketed number.
        readings = brackets.read_document(
            document(page(0, "In [1106] the court held"))
        )
        self.assertEqual(readings, {})

    def test_a_page_with_no_render_size_is_skipped(self):
        readings = brackets.read_document(
            document(page(0, "[7] The court", width=0))
        )
        self.assertEqual(readings, {})

    def test_a_cell_with_no_box_is_skipped(self):
        doc = document(page(0, "[7] The court"))
        doc["pages"][0]["cells"][0]["bbox"] = []
        self.assertEqual(brackets.read_document(doc), {})

    def test_no_document_reads_nothing(self):
        self.assertEqual(brackets.read_document(None), {})


class TestWriteRows(ScanningTestCase):
    """The rows the compute stores."""

    def setUp(self):
        super().setUp()
        self.scan = make_scan()

    def test_it_writes_one_row_per_reading_with_its_address(self):
        written = brackets.write_rows(
            self.scan, document(page(1, "[7] The court")), None
        )
        self.assertEqual(written, 1)
        row = BracketReading.objects.get(scan=self.scan)
        self.assertEqual(row.page_index, 1)
        self.assertEqual(row.source_page, 2)
        self.assertIsNone(row.source_edit_id)
        self.assertEqual(row.numbers, [7])
        self.assertEqual(row.bbox, CELL)
        self.assertEqual(row.source_fingerprint, self.scan.source_fingerprint)

    def test_a_second_compute_replaces_the_set(self):
        brackets.write_rows(
            self.scan, document(page(0, "[7] A", "[8] B")), None
        )
        brackets.write_rows(self.scan, document(page(0, "[7] A")), None)
        self.assertEqual(
            BracketReading.objects.filter(scan=self.scan).count(), 1
        )

    def test_no_document_leaves_the_rows_alone(self):
        brackets.write_rows(self.scan, document(page(0, "[7] A")), None)
        self.assertEqual(brackets.write_rows(self.scan, None, None), 0)
        self.assertEqual(
            BracketReading.objects.filter(scan=self.scan).count(), 1
        )

    def test_a_new_run_sweeps_the_readings_of_the_old_one(self):
        # ``missing`` reads the measured run alone, so the readings of
        # a superseded run are rows nothing will read again.
        brackets.write_rows(self.scan, document(page(0, "[7] A")), None)
        run = ApplyRun.objects.create(
            scan=self.scan,
            number=1,
            page_map={
                "pages": [{"source": {"kind": "original", "pdf_page": 1}}]
            },
        )
        brackets.write_rows(self.scan, document(page(0, "[7] A")), run)
        rows = BracketReading.objects.filter(scan=self.scan)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().apply_run_id, run.pk)


class TestTheRule(ScanningTestCase):
    """What ``missing`` yields, and what it rejects."""

    def setUp(self):
        super().setUp()
        self.scan = make_scan()
        caption = make_detection(self.scan, "CASE_CAPTION", page_index=0)
        key = make_detection(self.scan, "KEY_ICON", page_index=1)
        self.opinion = make_boundary(self.scan, caption, key)
        self.rows = [self.opinion]

    def found(self):
        return list(brackets.missing(self.scan, self.rows, None))

    def test_a_bracket_with_a_box_is_no_finding(self):
        brackets.write_rows(self.scan, document(page(0, "[7] A")), None)
        box_over(self.scan)
        self.assertEqual(self.found(), [])

    def test_a_bracket_the_opinion_already_names_is_no_finding(self):
        # The star-pagination case: the number is covered elsewhere, so
        # the token is not a headnote bracket.
        brackets.write_rows(
            self.scan,
            document(
                page(0, "[7] A"),
                page(1, ("[7] B", [200.0, 1000.0, 800.0, 1400.0])),
            ),
            None,
        )
        box_over(self.scan, page_index=0)
        self.assertEqual(self.found(), [])

    def test_a_bracket_that_fills_a_hole_is_a_finding(self):
        brackets.write_rows(
            self.scan,
            document(
                page(0, "[7] A"),
                page(1, ("[8] B", [200.0, 1000.0, 800.0, 1400.0])),
            ),
            None,
        )
        box_over(self.scan, page_index=0)
        found = self.found()
        self.assertEqual(len(found), 1)
        self.assertEqual(
            found[0]["check_name"], CheckName.MISSING_HEADNOTE_BRACKET
        )
        self.assertEqual(found[0]["page_number"], 2)
        self.assertEqual(found[0]["metadata"]["numbers"], [8])
        self.assertIn("8", found[0]["message"])

    def test_a_number_far_above_the_sequence_is_no_finding(self):
        brackets.write_rows(
            self.scan,
            document(
                page(0, "[2] A"),
                page(1, ("[40] B", [200.0, 1000.0, 800.0, 1400.0])),
            ),
            None,
        )
        box_over(self.scan, page_index=0)
        self.assertEqual(self.found(), [])

    def test_a_reading_outside_every_opinion_is_no_finding(self):
        brackets.write_rows(self.scan, document(page(3, "[7] A")), None)
        self.assertEqual(self.found(), [])

    def test_an_opinion_with_no_box_at_all_still_gives_a_finding(self):
        # The sequence says nothing, and the model missing every
        # bracket of an opinion is the fault the check exists for.
        brackets.write_rows(self.scan, document(page(0, "[7] A")), None)
        self.assertEqual(len(self.found()), 1)

    def test_a_deactivated_box_does_not_cover(self):
        brackets.write_rows(self.scan, document(page(0, "[7] A")), None)
        box_over(self.scan, active=False)
        self.assertEqual(len(self.found()), 1)

    def test_no_reading_gives_no_finding(self):
        self.assertEqual(self.found(), [])

    def test_a_page_two_opinions_share_splits_by_the_column(self):
        # The second opinion starts in the right column of page 1, so a
        # reading in the left column belongs to the first opinion and
        # one in the right column to the second. Ordering by y alone
        # would give both to the first.
        self.scan.detections.filter(label="TEXT_COLUMN").delete()
        for x0, x1 in ((100.0, 800.0), (900.0, 1600.0)):
            make_detection(
                self.scan,
                "TEXT_COLUMN",
                page_index=1,
                x0=x0,
                y0=100.0,
                x1=x1,
                y1=2000.0,
            )
        second = make_boundary(
            self.scan,
            make_detection(self.scan, "CASE_CAPTION", page_index=1),
            make_detection(self.scan, "KEY_ICON", page_index=1),
            start_page_index=1,
            start_x=900.0 * 72 / 200,
            start_y=200.0 * 72 / 200,
            end_page_index=1,
        )
        brackets.write_rows(
            self.scan,
            document(
                page(
                    1,
                    ("[3] Left", [200.0, 1500.0, 800.0, 1800.0]),
                    ("[1] Right", [950.0, 300.0, 1550.0, 600.0]),
                )
            ),
            None,
        )
        rows = {r.raw: r for r in BracketReading.objects.all()}
        ordered, columns = brackets._ordered_opinions(
            self.scan, [self.opinion, second], {1}
        )
        self.assertEqual(
            brackets.opinion_of(rows["[3]"], ordered, columns).pk,
            self.opinion.pk,
        )
        self.assertEqual(
            brackets.opinion_of(rows["[1]"], ordered, columns).pk,
            second.pk,
        )


class TestTheCard(ScanningTestCase):
    """The finding through ``findings.rebuild``, and its dismissal."""

    def setUp(self):
        super().setUp()
        self.scan = make_scan()
        caption = make_detection(self.scan, "CASE_CAPTION", page_index=0)
        key = make_detection(self.scan, "KEY_ICON", page_index=1)
        make_boundary(self.scan, caption, key)
        brackets.write_rows(
            self.scan,
            document(
                page(0, "[7] A"),
                page(1, ("[8] B", [200.0, 1000.0, 800.0, 1400.0])),
            ),
            None,
        )
        box_over(self.scan, page_index=0)

    def cards(self):
        return Issue.objects.filter(
            scan=self.scan,
            check_name=CheckName.MISSING_HEADNOTE_BRACKET,
        )

    def test_the_rebuild_writes_the_card(self):
        findings.rebuild(self.scan, run=None)
        self.assertEqual(self.cards().count(), 1)

    def test_drawing_the_box_takes_the_card_away(self):
        findings.rebuild(self.scan, run=None)
        box_over(
            self.scan,
            page_index=1,
            bbox=(210.0, 1010.0, 260.0, 1050.0),
            model_name=Detection.ModelName.MANUAL,
        )
        findings.rebuild(self.scan, run=None)
        self.assertEqual(self.cards().count(), 0)

    def test_a_dismissal_lands_on_the_card_again(self):
        findings.rebuild(self.scan, run=None)
        card = self.cards().get()
        findings.dismiss(self.scan, card, self.make_user())
        findings.rebuild(self.scan, run=None)
        card = self.cards().get()
        self.assertIsNotNone(card.dismissal_id)
        self.assertEqual(ReviewDismissal.objects.count(), 1)


class TestTheCardInTheViewer(ScanningTestCase):
    """The card names a box that is no row, and still highlights it."""

    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.scan = make_scan()
        caption = make_detection(self.scan, "CASE_CAPTION", page_index=0)
        key = make_detection(self.scan, "KEY_ICON", page_index=1)
        make_boundary(self.scan, caption, key)
        brackets.write_rows(self.scan, document(page(0, "[7] A")), None)
        findings.rebuild(self.scan, run=None)

    def test_the_card_carries_the_box_and_no_detection_id(self):
        response = self.client.get(
            reverse("scan_process", kwargs={"pk": self.scan.pk}), {"step": 2}
        )
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn(
            f'data-bbox="{CELL[0]},{CELL[1]},{CELL[2]},{CELL[3]}"', html
        )
        self.assertIn("headnote bracket 7", html)
        self.assertNotIn('data-detection-id="None"', html)


class TestTheCommand(ScanningTestCase):
    """``stamp_bracket_readings``, the backfill of the deploy."""

    def setUp(self):
        super().setUp()
        self.scan = make_scan(status=Status.READY_FOR_REDACTION_REVIEW)
        caption = make_detection(self.scan, "CASE_CAPTION", page_index=0)
        key = make_detection(self.scan, "KEY_ICON", page_index=1)
        make_boundary(self.scan, caption, key)
        self.document = document(
            page(0, "[7] A"),
            page(1, ("[8] B", [200.0, 1000.0, 800.0, 1400.0])),
        )
        box_over(self.scan, page_index=0)

    def run_command(self, *args):
        out = StringIO()
        with self.patched_document():
            call_command("stamp_bracket_readings", *args, stdout=out)
        return out.getvalue()

    def patched_document(self):
        from unittest.mock import patch

        return patch(
            "scanning.text_fit.load_document", return_value=self.document
        )

    def test_a_dry_run_writes_nothing(self):
        output = self.run_command("--dry-run")
        self.assertIn("Dry run", output)
        self.assertEqual(BracketReading.objects.count(), 0)
        self.assertEqual(Issue.objects.count(), 0)

    def test_it_writes_the_readings_and_the_card(self):
        output = self.run_command()
        self.assertIn("2 reading(s)", output)
        self.assertEqual(
            BracketReading.objects.filter(scan=self.scan).count(), 2
        )
        self.assertEqual(
            Issue.objects.filter(
                scan=self.scan,
                check_name=CheckName.MISSING_HEADNOTE_BRACKET,
            ).count(),
            1,
        )


class TestTheLineTokens(ScanningTestCase):
    """The bracket the OCR text of an opinion loses (#373)."""

    def strip(self, text):
        return brackets.strip_line_tokens(text)

    def test_the_bracket_that_opens_a_line_goes_with_its_space(self):
        self.assertEqual(
            self.strip("[1] The court held."), ("The court held.", ["[1]"])
        )

    def test_a_pair_and_a_range_go_too(self):
        self.assertEqual(self.strip("[1, 2] Double")[0], "Double")
        self.assertEqual(self.strip("[3–5] Equitable")[0], "Equitable")

    def test_every_line_of_a_joined_block_loses_its_bracket(self):
        self.assertEqual(
            self.strip("II\n[4, 5] The Second.\n[6] The third."),
            ("II\nThe Second.\nThe third.", ["[4, 5]", "[6]"]),
        )

    def test_the_space_before_the_bracket_stays(self):
        self.assertEqual(self.strip("  [7] Indented")[0], "  Indented")

    def test_a_bracket_inside_a_line_stays(self):
        """A footnote reference, or a bracket the page prints."""
        for text in (
            "the man inveigled[5] or kidnapped",
            "297 U.S. [157], at 160",
        ):
            self.assertEqual(self.strip(text), (text, []))

    def test_one_bracket_per_line(self):
        """The second token of the line is a star-pagination mark."""
        self.assertEqual(
            self.strip("[10] [22] Next, Evans argues"),
            ("[22] Next, Evans argues", ["[10]"]),
        )

    def test_a_number_that_is_no_headnote_stays(self):
        self.assertEqual(self.strip("[120] Providing access")[1], [])
        self.assertEqual(self.strip("[9-2] Nothing")[1], [])

    def test_the_bracket_alone_leaves_an_empty_text(self):
        self.assertEqual(self.strip("[3]"), ("", ["[3]"]))

    def test_a_bracket_that_fills_its_line_takes_the_newline(self):
        self.assertEqual(self.strip("[3]\nText"), ("Text", ["[3]"]))
        self.assertEqual(
            self.strip("II\n[4, 5]\nThe court"), ("II\nThe court", ["[4, 5]"])
        )

    def test_a_bracket_on_the_last_line_takes_the_newline_before(self):
        self.assertEqual(self.strip("Text\n[3]"), ("Text", ["[3]"]))
