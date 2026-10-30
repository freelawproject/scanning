"""The card review of review 2 (``card_review``): cards from rows alone.

Each builder reads one finding or one row and makes a card the page
draws as a crop with a box; ``cards`` orders them by group and page,
widens every crop to its column and attaches the page's other boxes.
The page writes nothing of its own, so the only endpoint of its own is
the list, plus ``restore_detection``, the Undo of a dismissed model box.
"""

import json
import pathlib
import re

from blackletter.models import Label
from blackletter.scanner import label_confidence
from django.urls import reverse

from scanning import card_review
from scanning.models import CheckName, Detection, DetectionDecision, Issue
from scanning.tests.test_findings import (
    make_detection,
    make_redaction,
    make_scan,
)
from scanning.tests.test_views import ScanningTestCase

APP = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = APP / "static" / "scanning" / "card_review.js"
TEMPLATE = APP / "templates" / "scanning" / "scan_card_review.html"


def finding(
    scan, check, metadata, page_index=0, target=Issue.Target.DETECTION
):
    return Issue.objects.create(
        scan=scan,
        severity=Issue.Severity.WARNING,
        check_name=check,
        target=target,
        page_number=page_index + 1,
        message="a card",
        metadata={
            "page_index": page_index,
            "img_width": 1700,
            "img_height": 2200,
            **metadata,
        },
    )


class TestTheBuilders(ScanningTestCase):
    def setUp(self):
        self.scan = make_scan()

    def test_a_bracket_card_is_the_guess_over_the_first_lines_of_the_cell(
        self,
    ):
        issue = finding(
            self.scan,
            CheckName.MISSING_HEADNOTE_BRACKET,
            {
                "bbox": [200.0, 1000.0, 800.0, 1400.0],
                "guess": [236.0, 1000.0, 290.0, 1033.0],
            },
        )
        card = card_review.bracket_card(issue)
        self.assertEqual(card["kind"], card_review.BRACKET)
        self.assertEqual(card["box"], [236.0, 1000.0, 290.0, 1033.0])
        self.assertEqual(card["label"], "HEADNOTE_BRACKET")
        self.assertEqual(card["issue_id"], issue.pk)
        # Five bracket heights of the cell, and the cell's width, padded.
        self.assertEqual(card["crop"], [160.0, 960.0, 840.0, 1238.0])
        self.assertTrue(
            card["key"].startswith("f:missing_headnote_bracket:0:")
        )

    def test_a_bracket_finding_with_no_guess_is_a_link(self):
        issue = finding(
            self.scan,
            CheckName.MISSING_HEADNOTE_BRACKET,
            {"bbox": [200.0, 1000.0, 800.0, 1400.0]},
        )
        card = card_review.bracket_card(issue)
        self.assertEqual(card["kind"], card_review.LINK)
        self.assertIsNone(card["box"])

    def test_a_faint_bracket_card_is_the_models_box(self):
        issue = finding(
            self.scan,
            CheckName.LOW_CONFIDENCE_HEADNOTE_BRACKET,
            {"bbox": [210.0, 1010.0, 260.0, 1050.0], "detection_id": 7},
        )
        card = card_review.faint_bracket_card(issue)
        self.assertEqual(card["kind"], card_review.FAINT)
        self.assertEqual(card["group"], card_review.BRACKET)
        self.assertEqual(card["detection_id"], 7)
        self.assertEqual(card["box"], [210.0, 1010.0, 260.0, 1050.0])

    def test_a_caption_card_draws_the_key_it_follows_when_in_its_column(self):
        issue = finding(
            self.scan,
            CheckName.MISSING_CAPTION,
            {
                "bbox": [228.0, 842.0, 814.0, 1325.0],
                "key_page_index": 0,
                "key_bbox": [446.0, 740.0, 617.0, 808.0],
            },
            target=Issue.Target.BOUNDARY,
        )
        card = card_review.caption_card(issue)
        self.assertEqual(card["kind"], card_review.CAPTION)
        self.assertEqual(card["label"], "CASE_CAPTION")
        self.assertEqual(card["anchor"], [446.0, 740.0, 617.0, 808.0])
        # The crop holds the key and the guess.
        self.assertLessEqual(card["crop"][1], 740.0)
        self.assertGreaterEqual(card["crop"][3], 1325.0)

    def test_a_first_key_caption_finding_is_a_link(self):
        issue = finding(
            self.scan,
            CheckName.MISSING_CAPTION,
            {"bbox": [100.0, 100.0, 340.0, 190.0]},
            target=Issue.Target.BOUNDARY,
        )
        card = card_review.caption_card(issue)
        self.assertEqual(card["kind"], card_review.LINK)

    def test_a_text_card_is_the_block_in_no_box(self):
        issue = finding(
            self.scan,
            CheckName.UNDETECTED_TEXT,
            {
                "bbox": [1000.0, 200.0, 1600.0, 600.0],
                "guess": [1000.0, 200.0, 1600.0, 600.0],
                "label": "TEXT_COLUMN",
                "label_id": int(Label.TEXT_COLUMN),
            },
        )
        card = card_review.text_card(issue)
        self.assertEqual(card["kind"], card_review.TEXT)
        self.assertEqual(card["group"], card_review.TEXT)
        self.assertEqual(card["box"], [1000.0, 200.0, 1600.0, 600.0])
        self.assertEqual(card["label"], "TEXT_COLUMN")
        self.assertEqual(card["label_id"], int(Label.TEXT_COLUMN))
        self.assertEqual(card["issue_id"], issue.pk)
        # The block and its pad.
        self.assertEqual(card["crop"], [960.0, 160.0, 1640.0, 640.0])
        self.assertTrue(card["key"].startswith("f:undetected_text:0:"))
        # The footnote answer takes the block itself.
        self.assertEqual(card["block"], card["box"])

    def test_a_text_finding_with_no_label_makes_no_card(self):
        issue = finding(
            self.scan,
            CheckName.UNDETECTED_TEXT,
            {"bbox": [1000.0, 200.0, 1600.0, 600.0]},
        )
        self.assertIsNone(card_review.text_card(issue))

    def test_the_text_cards_come_between_the_brackets_and_the_images(self):
        make_detection(
            self.scan,
            "IMAGE",
            0,
            label_id=int(Label.IMAGE),
            x0=300.0,
            y0=400.0,
            x1=800.0,
            y1=900.0,
        )
        finding(
            self.scan,
            CheckName.UNDETECTED_TEXT,
            {
                "bbox": [1000.0, 200.0, 1600.0, 600.0],
                "label": "TEXT_COLUMN",
                "label_id": int(Label.TEXT_COLUMN),
            },
        )
        finding(
            self.scan,
            CheckName.MISSING_HEADNOTE_BRACKET,
            {
                "bbox": [200.0, 1000.0, 800.0, 1400.0],
                "guess": [236.0, 1000.0, 290.0, 1033.0],
            },
        )
        self.assertEqual(
            [c["group"] for c in card_review.cards(self.scan)],
            ["bracket", "text", "image"],
        )

    def test_an_image_card_is_the_live_model_row(self):
        row = make_detection(
            self.scan,
            "IMAGE",
            2,
            label_id=int(Label.IMAGE),
            x0=300.0,
            y0=400.0,
            x1=1300.0,
            y1=1200.0,
            confidence=0.8,
        )
        card = card_review.image_card(row)
        self.assertEqual(card["kind"], card_review.IMAGE)
        self.assertEqual(card["detection_id"], row.pk)
        self.assertEqual(card["confidence"], 0.8)
        self.assertEqual(card["key"], f"d{row.pk}")
        self.assertEqual(card["page_number"], 3)

    def test_an_editorial_card_is_the_live_model_row(self):
        row = make_detection(
            self.scan,
            "EDITORIAL",
            1,
            label_id=int(Label.EDITORIAL),
            x0=300.0,
            y0=400.0,
            x1=800.0,
            y1=460.0,
            confidence=0.7,
        )
        card = card_review.editorial_card(row)
        self.assertEqual(card["kind"], card_review.EDITORIAL)
        self.assertEqual(card["group"], card_review.EDITORIAL)
        self.assertEqual(card["label"], "EDITORIAL")
        self.assertEqual(card["label_id"], int(Label.EDITORIAL))
        self.assertEqual(card["detection_id"], row.pk)
        self.assertEqual(card["key"], f"d{row.pk}")
        self.assertEqual(card["page_number"], 2)
        self.assertEqual(card["box"], [300.0, 400.0, 800.0, 460.0])
        # A few lines around the note, so it reads as whose it is.
        self.assertEqual(card["crop"], [240.0, 340.0, 860.0, 520.0])
        self.assertEqual(card["confidence"], 0.7)
        self.assertIn("editor's note box", card["message"])

    def test_the_editorial_cards_come_after_the_images(self):
        # The note is on an earlier page than the image: the group
        # orders before the page.
        make_detection(
            self.scan,
            "EDITORIAL",
            0,
            label_id=int(Label.EDITORIAL),
            x0=300.0,
            y0=100.0,
            x1=800.0,
            y1=160.0,
        )
        make_detection(
            self.scan,
            "IMAGE",
            1,
            label_id=int(Label.IMAGE),
            x0=300.0,
            y0=400.0,
            x1=800.0,
            y1=900.0,
        )
        self.assertEqual(
            [c["group"] for c in card_review.cards(self.scan)],
            ["image", "editorial"],
        )


class TestTheCards(ScanningTestCase):
    def setUp(self):
        self.scan = make_scan()

    def test_the_groups_the_columns_and_the_others(self):
        column = make_detection(
            self.scan,
            "TEXT_COLUMN",
            0,
            label_id=int(Label.TEXT_COLUMN),
            x0=90.0,
            y0=50.0,
            x1=850.0,
            y1=2000.0,
        )
        image = make_detection(
            self.scan,
            "IMAGE",
            0,
            label_id=int(Label.IMAGE),
            x0=300.0,
            y0=400.0,
            x1=800.0,
            y1=900.0,
        )
        finding(
            self.scan,
            CheckName.MISSING_HEADNOTE_BRACKET,
            {
                "bbox": [200.0, 1000.0, 800.0, 1400.0],
                "guess": [236.0, 1000.0, 290.0, 1033.0],
            },
        )
        dismissed = finding(
            self.scan,
            CheckName.MISSING_HEADNOTE_BRACKET,
            {
                "bbox": [200.0, 1500.0, 800.0, 1900.0],
                "guess": [236.0, 1500.0, 290.0, 1533.0],
            },
        )
        Issue.objects.filter(pk=dismissed.pk).update(dismissal=None)

        cards = card_review.cards(self.scan)

        self.assertEqual(
            [c["group"] for c in cards], ["bracket", "bracket", "image"]
        )
        bracket = cards[0]
        # Widened to the column box the guess sits in.
        self.assertEqual(bracket["crop"][0], 50.0)
        self.assertEqual(bracket["crop"][2], 890.0)
        # The page's other live boxes, the card's own left out.
        self.assertEqual(
            sorted(o["id"] for o in bracket["others"]),
            sorted([column.pk, image.pk]),
        )
        self.assertEqual([o["id"] for o in cards[2]["others"]], [column.pk])

    def test_a_model_image_row_an_approval_stands_on_makes_no_card(self):
        """An approval written before #414 left the model row live at
        1.0 with the decision on it: judged, so no card."""
        row = make_detection(self.scan, "IMAGE", 0, label_id=int(Label.IMAGE))
        decision = DetectionDecision.objects.create(
            scan=self.scan,
            kind=DetectionDecision.Kind.APPROVE,
            source_page=1,
            source_fingerprint=self.scan.source_fingerprint,
            label="IMAGE",
            label_id=int(Label.IMAGE),
            target_x0=row.x0,
            target_y0=row.y0,
            target_x1=row.x1,
            target_y1=row.y1,
            img_width=1700,
            img_height=2200,
        )
        Detection.objects.filter(pk=row.pk).update(
            decision=decision, confidence=1.0
        )
        self.assertEqual(card_review.cards(self.scan), [])

    def test_a_hand_drawn_image_row_makes_no_card(self):
        make_detection(
            self.scan,
            "IMAGE",
            0,
            label_id=int(Label.IMAGE),
            model_name=Detection.ModelName.MANUAL,
            found_by=[],
            confidence=1.0,
        )
        self.assertEqual(card_review.cards(self.scan), [])

    def test_a_text_card_names_the_box_beside_its_block_to_grow(self):
        """The page's footnote box ends at footnote 73 and the block is
        74-75 right under it: the card carries that box as the one to
        grow for the footnote answer; the column answer is always an
        add, a page having two column boxes by design."""
        footnotes = make_detection(
            self.scan,
            "FOOTNOTES",
            0,
            label_id=int(Label.FOOTNOTES),
            x0=100.0,
            y0=1500.0,
            x1=800.0,
            y1=1900.0,
        )
        make_detection(
            self.scan,
            "TEXT_COLUMN",
            0,
            label_id=int(Label.TEXT_COLUMN),
            x0=100.0,
            y0=200.0,
            x1=800.0,
            y1=1400.0,
        )
        finding(
            self.scan,
            CheckName.UNDETECTED_TEXT,
            {
                "bbox": [100.0, 1910.0, 780.0, 2000.0],
                "guess": [100.0, 1910.0, 780.0, 2000.0],
                "label": "TEXT_COLUMN",
                "label_id": int(Label.TEXT_COLUMN),
            },
        )

        (card,) = [
            c for c in card_review.cards(self.scan) if c["kind"] == "text"
        ]

        self.assertEqual(card["grow"]["footnote"]["id"], footnotes.pk)
        self.assertEqual(
            card["grow"]["footnote"]["grown"], [100.0, 1500.0, 800.0, 2000.0]
        )
        # The column and the editor's note answers always add: a page
        # has two column boxes, and a grown note box blacks out whatever
        # it reaches.
        self.assertEqual(set(card["grow"]), {"footnote"})

    def test_a_hand_drawn_editorial_row_makes_no_card(self):
        """A kept box is a hand-drawn row (#414): judged, so no card."""
        make_detection(
            self.scan,
            "EDITORIAL",
            0,
            label_id=int(Label.EDITORIAL),
            model_name=Detection.ModelName.MANUAL,
            found_by=[],
            confidence=1.0,
        )
        self.assertEqual(card_review.cards(self.scan), [])

    def note(self, **fields):
        """A model editor's note box, 500 by 60 pixels on page 1."""
        values = dict(
            label_id=int(Label.EDITORIAL),
            x0=300.0,
            y0=400.0,
            x1=800.0,
            y1=460.0,
        )
        values.update(fields)
        return make_detection(self.scan, "EDITORIAL", 0, **values)

    def test_an_editorial_box_inside_the_headnote_blackout_makes_no_card(
        self,
    ):
        """The headnote blackout runs from the synopsis to the start of
        the opinion, and a note box inside it is black whichever way it
        is answered: 84 in 100 of the boxes on thirty volumes."""
        self.note()
        # The note in points: [108, 144, 288, 166]. The blackout holds it.
        make_redaction(self.scan, 0, x0=100.0, y0=130.0, x1=300.0, y1=180.0)
        self.assertEqual(card_review.cards(self.scan), [])

    def test_a_white_box_or_another_note_box_is_no_cover(self):
        """A white strip hides nothing, and another editor's note box
        is itself in question; a blackout over half the box leaves the
        other half to this box."""
        row = self.note()
        make_redaction(
            self.scan,
            0,
            rect_type="margin",
            fill="white",
            x0=100.0,
            y0=130.0,
            x1=300.0,
            y1=180.0,
        )
        make_redaction(
            self.scan,
            0,
            rect_type="EDITORIAL",
            x0=100.0,
            y0=130.0,
            x1=300.0,
            y1=180.0,
        )
        make_redaction(self.scan, 0, x0=100.0, y0=130.0, x1=200.0, y1=180.0)
        self.assertEqual(
            [c["detection_id"] for c in card_review.cards(self.scan)],
            [row.pk],
        )

    def test_a_box_under_blackletters_gate_says_it_is_not_redacted(self):
        """blackletter draws a black box from a model detection only at
        or above its gate for the label and the model family; under it
        the note prints until the box is kept, so the card asks, and
        says so. The gate is read, never copied."""
        gate = label_confidence(Label.EDITORIAL, bl_warm=True)
        weak = self.note(confidence=round(gate - 0.01, 2))
        strong = self.note(confidence=gate, y0=600.0, y1=660.0)
        cards = {c["detection_id"]: c for c in card_review.cards(self.scan)}
        self.assertEqual(set(cards), {weak.pk, strong.pk})
        self.assertIn(
            f"under blackletter's gate of {gate:.2f}",
            cards[weak.pk]["message"],
        )
        self.assertIn("nothing is redacted yet", cards[weak.pk]["message"])
        self.assertNotIn("gate", cards[strong.pk]["message"])


class TestNeighbourBox(ScanningTestCase):
    """``neighbour_box``: the box a text card grows instead of adding."""

    BLOCK = [100.0, 1910.0, 780.0, 2000.0]

    def other(self, box, label="FOOTNOTES", pk=7):
        return {"id": pk, "label": label, "box": box, "own": False}

    def test_the_box_right_above_the_block_in_its_column_grows_over_it(
        self,
    ):
        """Footnotes 67-73 boxed, 74-75 printed right under the box:
        the box grows down to take them, one box, no seam."""
        found = card_review.neighbour_box(
            self.BLOCK,
            [self.other([100.0, 1500.0, 800.0, 1900.0])],
            "FOOTNOTES",
        )
        self.assertEqual(found["id"], 7)
        self.assertEqual(found["box"], [100.0, 1500.0, 800.0, 1900.0])
        self.assertEqual(found["grown"], [100.0, 1500.0, 800.0, 2000.0])

    def test_a_box_below_the_block_grows_up_over_it(self):
        found = card_review.neighbour_box(
            self.BLOCK,
            [self.other([100.0, 2020.0, 800.0, 2150.0])],
            "FOOTNOTES",
        )
        self.assertEqual(found["grown"], [100.0, 1910.0, 800.0, 2150.0])

    def test_a_box_too_far_or_in_the_other_column_is_no_neighbour(self):
        far = self.other([100.0, 1500.0, 800.0, 1700.0])
        other_column = self.other([900.0, 1500.0, 1600.0, 1900.0])
        self.assertIsNone(
            card_review.neighbour_box(
                self.BLOCK, [far, other_column], "FOOTNOTES"
            )
        )

    def test_a_box_beside_the_block_grows_across(self):
        """The footnote box covers the left column's notes and the block
        is the right column's, across the gutter: a page's footnotes
        are one block, so the box grows across."""
        found = card_review.neighbour_box(
            [420.0, 1800.0, 800.0, 2000.0],
            [self.other([100.0, 1800.0, 400.0, 2000.0])],
            "FOOTNOTES",
        )
        self.assertEqual(found["grown"], [100.0, 1800.0, 800.0, 2000.0])

    def test_the_label_must_match_and_the_nearest_wins(self):
        column = self.other(
            [100.0, 1890.0, 800.0, 1900.0], label="TEXT_COLUMN", pk=1
        )
        near = self.other([100.0, 1850.0, 800.0, 1905.0], pk=2)
        farther = self.other([100.0, 1500.0, 800.0, 1800.0], pk=3)
        self.assertIsNone(
            card_review.neighbour_box(self.BLOCK, [column], "FOOTNOTES")
        )
        self.assertEqual(
            card_review.neighbour_box(
                self.BLOCK, [farther, near], "FOOTNOTES"
            )["id"],
            2,
        )


class TestThePageFollowsTheGroups(ScanningTestCase):
    """The template's sections and the script's list are the groups of
    ``card_review.GROUPS``, in order, so a group added to the table is
    never a card the page has no section for."""

    def test_the_template_has_a_section_per_group(self):
        sections = re.findall(r'data-group="([a-z]+)"', TEMPLATE.read_text())
        self.assertEqual(sections, list(card_review.GROUPS))

    def test_the_script_lists_the_groups(self):
        source = SCRIPT.read_text()
        match = re.search(r"var GROUPS = \[([^\]]*)\];", source)
        self.assertIsNotNone(match)
        self.assertEqual(
            re.findall(r"'([a-z]+)'", match.group(1)), list(card_review.GROUPS)
        )

    def test_every_model_box_kind_has_its_verbs(self):
        """A card that holds a model box gets Keep and Take out from
        ``MODEL_VERBS``; a kind missing there would get the add
        buttons instead."""
        source = SCRIPT.read_text()
        for kind in (
            card_review.IMAGE,
            card_review.EDITORIAL,
            card_review.FAINT,
        ):
            self.assertRegex(source, rf"\n\s+{kind}: \{{ keep: ")


class TestTheEndpoints(ScanningTestCase):
    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.scan = make_scan()

    def _post(self, name, body):
        return self.client.post(
            reverse(name, kwargs={"pk": self.scan.pk}),
            data=json.dumps(body),
            content_type="application/json",
        )

    def test_the_page_and_its_cards(self):
        make_detection(self.scan, "IMAGE", 0, label_id=int(Label.IMAGE))
        page = self.client.get(
            reverse("scan_card_review", kwargs={"pk": self.scan.pk})
        )
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "card-review-config")
        cards = self.client.get(
            reverse("scan_review_cards", kwargs={"pk": self.scan.pk})
        )
        self.assertEqual(cards.status_code, 200)
        self.assertEqual([c["kind"] for c in cards.json()["cards"]], ["image"])

    def test_restore_brings_a_dismissed_model_box_back(self):
        row = make_detection(self.scan, "IMAGE", 0, label_id=int(Label.IMAGE))
        gone = self._post("delete_detection", {"detection_id": row.pk})
        self.assertEqual(gone.status_code, 200)
        row.refresh_from_db()
        self.assertFalse(row.active)

        back = self._post("restore_detection", {"detection_id": row.pk})

        self.assertEqual(back.status_code, 200)
        self.assertEqual(back.json()["restored"], 1)
        row.refresh_from_db()
        self.assertTrue(row.active)
        self.assertTrue(
            DetectionDecision.objects.filter(
                scan=self.scan, withdrawn_at__isnull=False
            ).exists()
        )

    def test_restore_refuses_a_hand_drawn_row(self):
        row = make_detection(
            self.scan,
            "IMAGE",
            0,
            label_id=int(Label.IMAGE),
            model_name=Detection.ModelName.MANUAL,
            found_by=[],
            confidence=1.0,
        )
        answer = self._post("restore_detection", {"detection_id": row.pk})
        self.assertEqual(answer.status_code, 409)

    def test_restore_refuses_a_model_box_a_move_replaced(self):
        """A move is a dismiss plus a hand-drawn row that names it: the
        dismiss alone taken back would put the model's box beside that
        row."""
        row = make_detection(self.scan, "IMAGE", 0, label_id=int(Label.IMAGE))
        moved = self._post(
            "update_detection",
            {"detection_id": row.pk, "new_bbox": [120.0, 120.0, 220.0, 220.0]},
        )
        self.assertEqual(moved.status_code, 200)
        row.refresh_from_db()
        self.assertFalse(row.active)

        answer = self._post("restore_detection", {"detection_id": row.pk})

        self.assertEqual(answer.status_code, 409)
        row.refresh_from_db()
        self.assertFalse(row.active)
        self.assertEqual(
            Detection.objects.live()
            .filter(scan=self.scan, label="IMAGE")
            .count(),
            1,
        )

    def test_a_dismissal_is_taken_back_by_its_own_id_after_a_rebuild(self):
        """The finding's pk dies at the next rebuild; the dismissal's
        does not."""
        from scanning import findings

        issue = finding(
            self.scan,
            CheckName.MISSING_HEADNOTE_BRACKET,
            {
                "bbox": [200.0, 1000.0, 800.0, 1400.0],
                "guess": [236.0, 1000.0, 290.0, 1033.0],
                "label": "HEADNOTE_BRACKET",
                "source_page": 1,
            },
        )
        gone = self._post("dismiss_finding", {"issue_id": issue.pk})
        self.assertEqual(gone.status_code, 200)
        dismissal_id = gone.json()["dismissal_id"]
        findings.rebuild(self.scan)
        self.assertFalse(Issue.objects.filter(pk=issue.pk).exists())

        stale = self._post("restore_finding", {"issue_id": issue.pk})
        self.assertEqual(stale.status_code, 404)
        back = self._post("restore_finding", {"dismissal_id": dismissal_id})
        self.assertEqual(back.status_code, 200)
        self.assertEqual(back.json()["restored"], True)

    def test_restore_on_a_live_model_box_says_so(self):
        row = make_detection(self.scan, "IMAGE", 0, label_id=int(Label.IMAGE))
        answer = self._post("restore_detection", {"detection_id": row.pk})
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.json()["restored"], 0)
