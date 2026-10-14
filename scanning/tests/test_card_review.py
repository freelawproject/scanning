"""The card review of review 2 (``card_review``): cards from rows alone.

Each builder reads one finding or one row and makes a card the page
draws as a crop with a box; ``cards`` orders them by group and page,
widens every crop to its column and attaches the page's other boxes.
The page writes nothing of its own, so the only endpoint of its own is
the list, plus ``restore_detection``, the Undo of a dismissed model box.
"""

import json

from blackletter.models import Label
from django.urls import reverse

from scanning import card_review
from scanning.models import CheckName, Detection, DetectionDecision, Issue
from scanning.tests.test_findings import make_detection, make_scan
from scanning.tests.test_views import ScanningTestCase


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
