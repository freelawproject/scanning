"""A whiteout pulled over model boxes dismisses them.

On a bleed-through page the curator pulls a margin whiteout over the
key icon, the caption and the headnote the model drew on the other
side of the leaf, and used to click Dismiss on each. The drag says
what the clicks said, so the write makes the same decisions: one
``deactivate`` per covered model box, each with its own Undo.
"""

import json

from django.urls import reverse

from scanning import detections, views_api
from scanning.models import Detection, DetectionDecision, Redaction
from scanning.tests.test_findings import make_detection, make_redaction, make_scan
from scanning.tests.test_views import ScanningTestCase


class TestCoveredShare(ScanningTestCase):
    """The measure of the rule."""

    def test_a_box_inside_the_cover_is_covered_whole(self):
        self.assertEqual(
            detections.covered_share([10, 10, 20, 20], [0, 0, 100, 100]), 1.0
        )

    def test_a_box_half_in_is_half_covered(self):
        self.assertAlmostEqual(
            detections.covered_share([0, 0, 20, 10], [10, 0, 100, 100]), 0.5
        )

    def test_an_empty_box_is_not_covered(self):
        self.assertEqual(
            detections.covered_share([5, 5, 5, 9], [0, 0, 100, 100]), 0.0
        )


class TestWhiteoutDismissesUnder(ScanningTestCase):
    """The two writes of a whiteout, and what they take with them."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.scan = make_scan()
        # A key icon at 100..340 x 100..190 render pixels of a 1700x2200
        # page: 36..122 x 36..68 points.
        self.key = make_detection(self.scan, "KEY_ICON")

    def _post(self, name, body, **kwargs):
        return self.client.post(
            reverse(name, kwargs={"pk": self.scan.pk, **kwargs}),
            data=json.dumps(body),
            content_type="application/json",
        )

    def _cover(self, fill="white"):
        """A box over the key icon with room to spare, in points."""
        return {"x0": 20.0, "y0": 20.0, "x1": 140.0, "y1": 90.0, "fill": fill}

    def test_a_whiteout_drawn_over_a_model_box_dismisses_it(self):
        response = self._post("add_redaction", {"page_index": 0, **self._cover()})
        self.assertEqual(response.status_code, 200)
        answer = response.json()
        self.assertEqual(answer["dismissed"], [self.key.pk])
        self.assertEqual(
            answer["message"],
            views_api.SAVED_REDACTION_MESSAGE
            + " 1 model box under the whiteout was dismissed.",
        )
        self.key.refresh_from_db()
        self.assertFalse(self.key.active)
        decision = DetectionDecision.objects.get()
        self.assertEqual(decision.kind, DetectionDecision.Kind.DEACTIVATE)
        self.assertEqual(decision.author, self.user)

    def test_a_whiteout_moved_over_a_model_box_dismisses_it(self):
        row = make_redaction(
            self.scan,
            origin=Redaction.Origin.HUMAN,
            kind=Redaction.Kind.ADD,
            rect_type=Redaction.MANUAL_TYPE,
            fill=Redaction.Fill.WHITE,
            x0=300.0,
            y0=300.0,
            x1=400.0,
            y1=400.0,
            author=self.user,
        )
        body = self._cover()
        del body["fill"]
        response = self._post("move_redaction", body, redaction_id=row.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["dismissed"], [self.key.pk])
        self.assertIn("1 model box under the whiteout was dismissed", response.json()["message"])
        self.key.refresh_from_db()
        self.assertFalse(self.key.active)

    def test_two_boxes_make_two_decisions_and_say_so(self):
        caption = make_detection(
            self.scan, "CASE_CAPTION", x0=100.0, y0=200.0, x1=340.0, y1=240.0
        )
        body = self._cover()
        body["y1"] = 100.0
        response = self._post("add_redaction", {"page_index": 0, **body})
        self.assertEqual(
            sorted(response.json()["dismissed"]), sorted([self.key.pk, caption.pk])
        )
        self.assertIn("2 model boxes under the whiteout were dismissed", response.json()["message"])
        self.assertEqual(DetectionDecision.objects.count(), 2)

    def test_a_black_box_dismisses_nothing(self):
        response = self._post(
            "add_redaction", {"page_index": 0, **self._cover(fill="black")}
        )
        self.assertEqual(response.json()["dismissed"], [])
        self.assertEqual(response.json()["message"], views_api.SAVED_REDACTION_MESSAGE)
        self.key.refresh_from_db()
        self.assertTrue(self.key.active)

    def test_a_box_the_whiteout_only_touches_stays(self):
        cover = self._cover()
        cover["x1"] = 80.0  # about half of the key's width
        self._post("add_redaction", {"page_index": 0, **cover})
        self.key.refresh_from_db()
        self.assertTrue(self.key.active)
        self.assertFalse(DetectionDecision.objects.exists())

    def test_a_hand_drawn_box_stays(self):
        self.key.model_name = Detection.ModelName.MANUAL
        self.key.found_by = []
        self.key.save(update_fields=["model_name", "found_by"])
        response = self._post("add_redaction", {"page_index": 0, **self._cover()})
        self.assertEqual(response.json()["dismissed"], [])
        self.key.refresh_from_db()
        self.assertTrue(self.key.active)

    def test_a_box_of_the_running_head_stays(self):
        for label in sorted(detections.HEAD_LINE_LABELS):
            with self.subTest(label=label):
                self.key.label = label
                self.key.save(update_fields=["label"])
                self._post("add_redaction", {"page_index": 0, **self._cover()})
                self.key.refresh_from_db()
                self.assertTrue(self.key.active)

    def test_a_box_on_another_page_stays(self):
        other = make_detection(self.scan, "KEY_ICON", page_index=1)
        self._post("add_redaction", {"page_index": 0, **self._cover()})
        other.refresh_from_db()
        self.assertTrue(other.active)

    def test_an_already_dismissed_box_is_not_dismissed_again(self):
        detections.decide(
            self.scan, self.key, DetectionDecision.Kind.DEACTIVATE, self.user
        )
        response = self._post("add_redaction", {"page_index": 0, **self._cover()})
        self.assertEqual(response.json()["dismissed"], [])
        self.assertEqual(DetectionDecision.objects.count(), 1)

    def test_the_undo_is_the_restore_of_each_box(self):
        self._post("add_redaction", {"page_index": 0, **self._cover()})
        response = self._post("restore_detection", {"detection_id": self.key.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["restored"], 1)
        self.key.refresh_from_db()
        self.assertTrue(self.key.active)
