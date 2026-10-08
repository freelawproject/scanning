"""Tests for the editor's notes no redaction covers (issue #450).

``editorial`` has two halves, the shape of ``brackets``.
:func:`editorial.write_rows` runs in the compute and stores one
``EditorialReading`` row per note the reader found;
:func:`editorial.uncovered` runs in ``findings.rebuild``, reads rows
only, and writes a finding for a note that no black redaction and no
redactable ``EDITORIAL`` box covers. Tested here: the note, its band,
the reading of a document, the rows, the rule, the card through the
rebuild, and the backfill command.
"""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command

from scanning import editorial, findings, redactions
from scanning.models import (
    DISMISSABLE_REVIEW2_CHECKS,
    CheckName,
    Detection,
    EditorialReading,
    Issue,
    Redaction,
    ReviewDismissal,
    Status,
)
from scanning.tests.test_brackets import CELL, document, page
from scanning.tests.test_findings import (
    make_boundary,
    make_detection,
    make_redaction,
    make_scan,
)
from scanning.tests.test_views import ScanningTestCase

#: A note that fills its cell, so its band is :data:`CELL`.
NOTE = "[Editor's Note: The preceding image contains footnote 3]"

#: :data:`CELL` in points: 0.36 of a 200 dpi pixel.
CELL_PT = (72.0, 144.0, 288.0, 324.0)


def with_opinion(scan):
    """Give ``scan`` one computed boundary, so the rebuild measures."""
    caption = make_detection(scan, "CASE_CAPTION", page_index=0)
    key = make_detection(scan, "KEY_ICON", page_index=1)
    return make_boundary(scan, caption, key)


def editorial_box(scan, page_index=0, **fields):
    """Create a live ``EDITORIAL`` row over :data:`CELL`."""
    values = {
        "label_id": 14,
        "x0": 190.0,
        "y0": 390.0,
        "x1": 810.0,
        "y1": 910.0,
    }
    values.update(fields)
    return make_detection(
        scan, editorial.EDITORIAL_LABEL, page_index=page_index, **values
    )


def manual_box(scan, **fields):
    """Create a hand-drawn ``EDITORIAL`` row over :data:`CELL`."""
    values = {"confidence": 1.0, "model_name": Detection.ModelName.MANUAL}
    values.update(fields)
    return editorial_box(scan, **values)


class TestTheNote(ScanningTestCase):
    """What the reader writes, and what counts as a note."""

    def test_a_bracketed_note_is_found_to_its_closing_bracket(self):
        text = NOTE + ". As shown above"
        self.assertEqual(editorial.find_notes(text), [(0, len(NOTE))])

    def test_a_note_inside_a_paragraph_is_found(self):
        text = "Body. " + NOTE
        self.assertEqual(editorial.find_notes(text), [(6, len(text))])

    def test_two_notes_in_one_cell_are_both_found(self):
        text = NOTE + " Body. " + NOTE
        second = len(NOTE) + len(" Body. ")
        self.assertEqual(
            editorial.find_notes(text),
            [(0, len(NOTE)), (second, len(text))],
        )

    def test_the_curly_apostrophe_and_none_are_read(self):
        self.assertTrue(editorial.find_notes("[Editor’s Note: x]"))
        self.assertTrue(editorial.find_notes("[Editors Note: x]"))
        self.assertTrue(editorial.find_notes("[ EDITOR'S NOTE: x]"))

    def test_a_note_with_no_bracket_is_not_read(self):
        # A court that quotes a statute's annotation writes no bracket.
        self.assertEqual(
            editorial.find_notes("The editor's note to § 12 says so."), []
        )

    def test_a_note_with_no_closing_bracket_runs_to_the_cap(self):
        text = "[Editor's Note: " + "x" * 1000
        self.assertEqual(
            editorial.find_notes(text), [(0, editorial.MAX_NOTE_CHARS)]
        )


class TestTheBand(ScanningTestCase):
    """The note's share of its cell."""

    def test_a_note_that_fills_the_cell_is_the_cell(self):
        self.assertEqual(
            editorial.note_band(tuple(CELL), 0, 10, 10), tuple(CELL)
        )

    def test_the_first_quarter_of_the_text_is_the_top_quarter(self):
        self.assertEqual(
            editorial.note_band((200.0, 400.0, 800.0, 800.0), 0, 20, 80),
            (200.0, 400.0, 800.0, 500.0),
        )


class TestReadDocument(ScanningTestCase):
    """The readings of one glued OCR document."""

    def test_a_note_is_read_with_its_band(self):
        text = NOTE + "x" * len(NOTE)
        readings = editorial.read_document(document(page(0, text)))
        self.assertEqual(len(readings[0]), 1)
        reading = readings[0][0]
        self.assertEqual(reading.text, NOTE)
        self.assertEqual(reading.bbox, (200.0, 400.0, 800.0, 650.0))

    def test_a_cell_with_no_note_is_not_read(self):
        readings = editorial.read_document(document(page(0, "[7] A")))
        self.assertEqual(readings, {})

    def test_a_page_with_no_render_size_is_skipped(self):
        readings = editorial.read_document(document(page(0, NOTE, width=0)))
        self.assertEqual(readings, {})

    def test_no_document_reads_nothing(self):
        self.assertEqual(editorial.read_document(None), {})


class TestWriteRows(ScanningTestCase):
    """The rows the compute stores."""

    def setUp(self):
        super().setUp()
        self.scan = make_scan()

    def test_it_writes_one_row_per_note_with_its_address(self):
        written = editorial.write_rows(
            self.scan, document(page(1, NOTE)), None
        )
        self.assertEqual(written, 1)
        row = EditorialReading.objects.get(scan=self.scan)
        self.assertEqual(row.page_index, 1)
        self.assertEqual(row.source_page, 2)
        self.assertIsNone(row.source_edit_id)
        self.assertEqual(row.text, NOTE)
        self.assertEqual(row.bbox, CELL)
        self.assertEqual(row.source_fingerprint, self.scan.source_fingerprint)

    def test_a_second_compute_replaces_the_set(self):
        editorial.write_rows(
            self.scan, document(page(0, NOTE), page(1, NOTE)), None
        )
        editorial.write_rows(self.scan, document(page(0, NOTE)), None)
        self.assertEqual(
            EditorialReading.objects.filter(scan=self.scan).count(), 1
        )

    def test_no_document_leaves_the_rows_alone(self):
        editorial.write_rows(self.scan, document(page(0, NOTE)), None)
        self.assertEqual(editorial.write_rows(self.scan, None, None), 0)
        self.assertEqual(
            EditorialReading.objects.filter(scan=self.scan).count(), 1
        )


class TestTheRule(ScanningTestCase):
    """What ``uncovered`` yields, and what it rejects."""

    def setUp(self):
        super().setUp()
        self.scan = make_scan()
        editorial.write_rows(self.scan, document(page(0, NOTE)), None)

    def found(self):
        return list(editorial.uncovered(self.scan, None))

    def test_a_note_nothing_covers_is_a_finding(self):
        found = self.found()
        self.assertEqual(len(found), 1)
        self.assertEqual(
            found[0]["check_name"], CheckName.UNCOVERED_EDITORS_NOTE
        )
        self.assertEqual(found[0]["target"], Issue.Target.REDACTION)
        self.assertEqual(found[0]["page_number"], 1)
        self.assertEqual(found[0]["metadata"]["bbox"], CELL)
        self.assertEqual(found[0]["metadata"]["source_page"], 1)

    def test_a_black_redaction_over_it_covers_it(self):
        x0, y0, x1, y1 = CELL_PT
        make_redaction(self.scan, x0=x0, y0=y0, x1=x1, y1=y1)
        self.assertEqual(self.found(), [])

    def test_a_white_redaction_does_not_cover_it(self):
        x0, y0, x1, y1 = CELL_PT
        make_redaction(
            self.scan,
            x0=x0,
            y0=y0,
            x1=x1,
            y1=y1,
            rect_type="margin",
            fill=Redaction.Fill.WHITE,
        )
        self.assertEqual(len(self.found()), 1)

    def test_a_black_redaction_on_another_page_does_not_cover_it(self):
        x0, y0, x1, y1 = CELL_PT
        make_redaction(self.scan, page_index=1, x0=x0, y0=y0, x1=x1, y1=y1)
        self.assertEqual(len(self.found()), 1)

    def test_a_model_editorial_box_alone_does_not_cover_it(self):
        # The readings are written after the model boxes had their
        # redactions, so a model box with no black box under it is one
        # whose redaction did not happen.
        editorial_box(self.scan, confidence=0.9)
        self.assertEqual(len(self.found()), 1)

    def test_a_dismissed_redaction_under_a_model_box_gives_a_card(self):
        editorial_box(self.scan, confidence=0.9)
        x0, y0, x1, y1 = CELL_PT
        row = make_redaction(
            self.scan, x0=x0, y0=y0, x1=x1, y1=y1, rect_type="EDITORIAL"
        )
        self.assertEqual(self.found(), [])
        redactions.dismiss(self.scan, row, None)
        self.assertEqual(len(self.found()), 1)

    def test_a_hand_drawn_editorial_box_covers_it(self):
        manual_box(self.scan)
        self.assertEqual(self.found(), [])

    def test_a_tight_box_just_short_of_the_centre_covers_it(self):
        # The centre of CELL is y 650, and the pad is 11 px of 2200.
        manual_box(self.scan, y1=640.0)
        self.assertEqual(self.found(), [])

    def test_a_box_over_another_part_of_the_cell_does_not_cover_it(self):
        manual_box(self.scan, y1=600.0)
        self.assertEqual(len(self.found()), 1)

    def test_a_withdrawn_hand_drawn_box_does_not_cover_it(self):
        manual_box(self.scan, active=False)
        self.assertEqual(len(self.found()), 1)

    def test_a_box_of_another_label_does_not_cover_it(self):
        make_detection(
            self.scan, "TEXT_COLUMN", x0=190.0, y0=390.0, x1=810.0, y1=910.0
        )
        self.assertEqual(len(self.found()), 1)

    def test_no_reading_gives_no_finding(self):
        EditorialReading.objects.all().delete()
        self.assertEqual(self.found(), [])


class TestTheCard(ScanningTestCase):
    """The finding through ``findings.rebuild``, and its dismissal."""

    def setUp(self):
        super().setUp()
        self.scan = make_scan()
        with_opinion(self.scan)
        editorial.write_rows(self.scan, document(page(0, NOTE)), None)

    def cards(self):
        return Issue.objects.filter(
            scan=self.scan, check_name=CheckName.UNCOVERED_EDITORS_NOTE
        )

    def test_the_check_is_a_dismissable_review2_check(self):
        self.assertIn(
            CheckName.UNCOVERED_EDITORS_NOTE, DISMISSABLE_REVIEW2_CHECKS
        )

    def test_the_rebuild_writes_the_card(self):
        findings.rebuild(self.scan, run=None)
        self.assertEqual(self.cards().count(), 1)

    def test_no_computed_boundary_writes_no_card(self):
        # The rule of every measured finding: a volume the compute has
        # not reached has no redactions, so every note would read as
        # uncovered.
        self.scan.opinion_boundaries.all().delete()
        findings.rebuild(self.scan, run=None)
        self.assertEqual(self.cards().count(), 0)

    def test_drawing_the_box_takes_the_card_away(self):
        findings.rebuild(self.scan, run=None)
        manual_box(self.scan)
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


class TestTheCommand(ScanningTestCase):
    """``stamp_bracket_readings`` writes the notes of the backfill too."""

    def setUp(self):
        super().setUp()
        self.scan = make_scan(status=Status.READY_FOR_REDACTION_REVIEW)
        with_opinion(self.scan)

    def run_command(self, *args):
        out = StringIO()
        with patch(
            "scanning.text_fit.load_document",
            return_value=document(page(0, NOTE)),
        ):
            call_command("stamp_bracket_readings", *args, stdout=out)
        return out.getvalue()

    def test_a_dry_run_writes_nothing(self):
        output = self.run_command("--dry-run")
        self.assertIn("1 editor's note(s), 1 uncovered", output)
        self.assertEqual(EditorialReading.objects.count(), 0)
        self.assertEqual(Issue.objects.count(), 0)

    def test_it_writes_the_reading_and_the_card(self):
        self.run_command()
        self.assertEqual(
            EditorialReading.objects.filter(scan=self.scan).count(), 1
        )
        self.assertEqual(
            Issue.objects.filter(
                scan=self.scan, check_name=CheckName.UNCOVERED_EDITORS_NOTE
            ).count(),
            1,
        )
