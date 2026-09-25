"""Tests for the success line of every review-2 write (issue #322).

A curator moved a margin box and the page showed nothing: the box
stays where the mouse left it after a save and after a refusal, so the
one record of the answer was the toast the viewer never showed.

Every write of review 2 now answers ``{status: "ok", message}``, and
the viewer shows the message as a success toast. The text lives in the
view, which knows what it wrote, never in the viewer scripts.

Three pins here: the set of the write views, the message in every one
of their success answers, and the absence of a success line in the two
viewer scripts.
"""

import ast
import json
import pathlib
import re

from django.urls import reverse

from scanning import findings, views_api
from scanning.models import (
    Detection,
    DetectionDecision,
    OpinionBoundary,
    Redaction,
    ReviewDismissal,
)
from scanning.tests.test_findings import (
    make_boundary,
    make_detection,
    make_redaction,
    make_scan,
)
from scanning.tests.test_views import DetectionEndpointMixin, ScanningTestCase

#: The writes of review 2, one line each. A new one joins this tuple by
#: hand: the test below derives the same set from the code and fails
#: until an author adds it, which is the moment to write its message.
WRITE_VIEWS = (
    "add_boundary",
    "add_redaction",
    "add_single_detection",
    "approve_detection",
    "delete_detection",
    "dismiss_boundary",
    "dismiss_finding",
    "dismiss_redaction",
    "move_redaction",
    "rebuild_findings",
    "restore_boundary",
    "restore_finding",
    "restore_redaction",
    "update_detection",
    "withdraw_stale_edit",
)

#: The writes of review 3 (#419). Apart, because ``test_detection_preview``
#: holds every name of ``WRITE_VIEWS`` to the review-2 gate, and a
#: review-3 write has a gate of its own: the opinion's status.
REVIEW3_WRITE_VIEWS = (
    "approve_opinion_text",
    "dismiss_opinion_finding",
    "reopen_opinion_text",
    "restore_opinion_finding",
)

#: The human edits of review 3 (#376). Each one answers through
#: ``_build_after_edit``, which writes the Django message and the JSON
#: of the success, so the pin reads the answers of that helper.
REVIEW3_EDIT_VIEWS = (
    "edit_opinion_section",
    "edit_opinion_text",
    "move_opinion_block",
    "withdraw_opinion_edit",
)

#: The calls that make a function a write of review 2: it rebuilds the
#: findings, or it writes a curator's dismissal of one.
WRITE_CALLS = (
    "_rebuild_findings",
    "approve_text",
    "reopen_text",
    "dismiss",
    "restore",
    "withdraw_stale",
    "rebuild",
    "supersede",
    "withdraw",
)

VIEWS_PATH = pathlib.Path("scanning/views_api.py")
SCRIPTS = (
    pathlib.Path("scanning/static/scanning/viewer_step2.js"),
    pathlib.Path("scanning/static/scanning/viewer_sidebar.js"),
    pathlib.Path("scanning/static/scanning/viewer_step3.js"),
)
SHARED = pathlib.Path("scanning/static/scanning/shared.js")


def _write_functions():
    """Read the write views of review 2 out of the view module.

    :returns: The function nodes, by name.
    :rtype: dict
    """
    tree = ast.parse(VIEWS_PATH.read_text())
    found = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name.startswith("_"):
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            name = getattr(call.func, "attr", getattr(call.func, "id", ""))
            if name in WRITE_CALLS:
                found[node.name] = node
                break
    return found


def _function(name):
    """Return one function of the view module, by name."""
    tree = ast.parse(VIEWS_PATH.read_text())
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _calls(node) -> set:
    """Return the names every call inside one function calls."""
    return {
        getattr(call.func, "attr", getattr(call.func, "id", ""))
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
    }


def _success_answers(node):
    """Read the success answers of one view.

    :param node: The function node.
    :returns: The key sets of every ``JsonResponse`` dict that carries
        ``status`` "ok".
    :rtype: list
    """
    answers = []
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        if getattr(call.func, "id", "") != "JsonResponse":
            continue
        if not call.args or not isinstance(call.args[0], ast.Dict):
            continue
        keys = {
            key.value
            for key in call.args[0].keys
            if isinstance(key, ast.Constant)
        }
        values = {
            value.value
            for key, value in zip(
                call.args[0].keys, call.args[0].values, strict=True
            )
            if isinstance(key, ast.Constant)
            and key.value == "status"
            and isinstance(value, ast.Constant)
        }
        if "ok" in values:
            answers.append(keys)
    return answers


class TestEveryWriteAnswersAMessage(ScanningTestCase):
    """The set of the write views, and the message in each answer."""

    def test_the_write_views_are_the_pinned_set(self):
        self.assertEqual(
            sorted(_write_functions()),
            sorted(WRITE_VIEWS + REVIEW3_WRITE_VIEWS + REVIEW3_EDIT_VIEWS),
        )

    def test_every_edit_answers_through_the_one_helper(self):
        """An edit of review 3 answers a Django message, success or
        error, and the page reloads to show it (#376)."""
        functions = _write_functions()
        for name in REVIEW3_EDIT_VIEWS:
            calls = _calls(functions[name])
            self.assertIn("_build_after_edit", calls, name)
            self.assertNotIn("JsonResponse", calls, f"{name} answers alone")
        helper = _function("_build_after_edit")
        answers = _success_answers(helper)
        self.assertTrue(answers)
        for keys in answers:
            self.assertIn("message", keys)
        self.assertIn("success", _calls(helper))
        self.assertIn("warning", _calls(helper))
        self.assertIn("error", _calls(_function("_edit_refusal")))

    def test_every_success_answer_carries_a_message(self):
        functions = _write_functions()
        for name in WRITE_VIEWS + REVIEW3_WRITE_VIEWS:
            answers = _success_answers(functions[name])
            self.assertTrue(answers, f"{name} answers no success dict")
            for keys in answers:
                self.assertIn("message", keys, f"{name} answers no message")


class TestTheTextLivesInTheView(ScanningTestCase):
    """The scripts show the server's line and write none of their own."""

    def test_the_viewer_scripts_carry_no_success_line(self):
        pattern = re.compile(
            r"showToast\(\s*['\"][^'\"]*['\"]\s*,\s*['\"]success['\"]"
        )
        for path in SCRIPTS:
            self.assertIsNone(
                pattern.search(path.read_text()),
                f"{path.name} writes a success line of its own",
            )

    def test_the_viewer_scripts_show_the_server_line(self):
        """The helper is called with the answer, never with a string.

        A viewer shows the line the write view wrote. Two helpers do
        that: ``showSaved`` on the page, and ``showSavedAfterReload``
        over a reload. Either one must take a value of the answer, so a
        literal in the script is what this pin catches.
        """
        pattern = re.compile(r"showSaved(?:AfterReload)?\(\s*[A-Za-z_$]")
        for path in SCRIPTS:
            self.assertIsNotNone(
                pattern.search(path.read_text()),
                f"{path.name} shows no line of the server",
            )

    def test_the_shared_script_defines_the_three_helpers(self):
        text = SHARED.read_text()
        for name in (
            "function showSaved(",
            "function showSavedAfterReload(",
            "function flushSavedToast(",
        ):
            self.assertIn(name, text)


class TestThePairingLabels(ScanningTestCase):
    """The two labels the added-box message reads.

    The view keeps them as strings, because the message is the only
    reader and the label names are the worker's classes, the viewer's
    colour table and blackletter's enum already. This pin is what a
    rename in the enum breaks, so the two sides cannot drift apart.
    """

    def test_they_are_the_names_of_the_two_anchor_labels(self):
        from blackletter.models import Label

        self.assertEqual(
            views_api.PAIRING_LABELS,
            (Label.CASE_CAPTION.name, Label.KEY_ICON.name),
        )


class TestRedactionMessages(ScanningTestCase):
    """The lines of the four redaction writes."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.scan = make_scan()

    def _post(self, name, body=None, **kwargs):
        return self.client.post(
            reverse(name, kwargs={"pk": self.scan.pk, **kwargs}),
            data=json.dumps(body or {}),
            content_type="application/json",
        )

    def _box(self):
        return {"x0": 10.0, "y0": 20.0, "x1": 60.0, "y1": 70.0}

    def test_the_add_names_the_measurement_it_did_not_run(self):
        response = self._post(
            "add_redaction", {"page_index": 0, **self._box()}
        )

        self.assertEqual(
            response.json()["message"], views_api.SAVED_REDACTION_MESSAGE
        )

    def test_a_moved_computed_box_says_the_curator_holds_it_now(self):
        row = make_redaction(self.scan)

        response = self._post(
            "move_redaction", self._box(), redaction_id=row.pk
        )

        self.assertEqual(
            response.json()["message"], views_api.MOVED_OVER_COMPUTED_MESSAGE
        )

    def test_a_moved_human_box_says_only_that_it_moved(self):
        row = make_redaction(
            self.scan,
            origin=Redaction.Origin.HUMAN,
            kind=Redaction.Kind.ADD,
            rect_type=Redaction.MANUAL_TYPE,
            author=self.user,
        )

        response = self._post(
            "move_redaction", self._box(), redaction_id=row.pk
        )

        self.assertEqual(
            response.json()["message"], views_api.MOVED_BOX_MESSAGE
        )

    def test_a_computed_box_is_dismissed_and_a_human_one_withdrawn(self):
        computed = make_redaction(self.scan)
        human = make_redaction(
            self.scan,
            origin=Redaction.Origin.HUMAN,
            kind=Redaction.Kind.ADD,
            rect_type=Redaction.MANUAL_TYPE,
            author=self.user,
        )

        self.assertEqual(
            self._post("dismiss_redaction", redaction_id=computed.pk).json()[
                "message"
            ],
            views_api.DISMISSED_REDACTION_MESSAGE,
        )
        self.assertEqual(
            self._post("dismiss_redaction", redaction_id=human.pk).json()[
                "message"
            ],
            views_api.WITHDRAWN_REDACTION_MESSAGE,
        )

    def test_the_restore_says_whether_a_dismissal_stood(self):
        row = make_redaction(self.scan)
        self._post("dismiss_redaction", redaction_id=row.pk)

        self.assertEqual(
            self._post("restore_redaction", redaction_id=row.pk).json()[
                "message"
            ],
            views_api.RESTORED_REDACTION_MESSAGE,
        )
        self.assertEqual(
            self._post("restore_redaction", redaction_id=row.pk).json()[
                "message"
            ],
            views_api.STANDING_REDACTION_MESSAGE,
        )


class TestDetectionMessages(DetectionEndpointMixin, ScanningTestCase):
    """The lines of the four detection writes."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)

    def test_a_moved_model_box_says_the_curator_holds_it_now(self):
        scan, det = self._make_scan_with_detection()

        response = self._post(
            "update_detection",
            scan,
            {"detection_id": det.pk, "new_bbox": [10, 10, 60, 60]},
        )

        self.assertEqual(
            response.json()["message"], views_api.MOVED_OVER_MODEL_MESSAGE
        )

    def test_a_moved_hand_drawn_box_says_only_that_it_moved(self):
        scan, det = self._make_scan_with_detection(
            model_name=Detection.ModelName.MANUAL, found_by=[]
        )

        response = self._post(
            "update_detection",
            scan,
            {"detection_id": det.pk, "new_bbox": [10, 10, 60, 60]},
        )

        self.assertEqual(
            response.json()["message"], views_api.MOVED_BOX_MESSAGE
        )

    def test_a_model_row_is_dismissed_and_a_hand_drawn_one_withdrawn(self):
        scan, model = self._make_scan_with_detection()

        self.assertEqual(
            self._post(
                "delete_detection", scan, {"detection_id": model.pk}
            ).json()["message"],
            views_api.DISMISSED_DETECTION_MESSAGE,
        )

        scan, manual = self._make_scan_with_detection(
            model_name=Detection.ModelName.MANUAL, found_by=[]
        )

        self.assertEqual(
            self._post(
                "delete_detection", scan, {"detection_id": manual.pk}
            ).json()["message"],
            views_api.WITHDRAWN_DETECTION_MESSAGE,
        )

    def test_the_approval_names_the_pairing_that_did_not_run(self):
        scan, det = self._make_scan_with_detection()

        response = self._post(
            "approve_detection", scan, {"detection_id": det.pk}
        )

        self.assertEqual(
            response.json()["message"], views_api.APPROVED_DETECTION_MESSAGE
        )

    def test_the_approval_of_a_bracket_names_the_compute(self):
        """#410: a bracket approved from its card is over the gate now."""
        scan, det = self._make_scan_with_detection(
            label="HEADNOTE_BRACKET", confidence=0.25
        )

        response = self._post(
            "approve_detection", scan, {"detection_id": det.pk}
        )

        self.assertEqual(
            response.json()["message"], views_api.APPROVED_BRACKET_MESSAGE
        )

    def test_a_hand_drawn_row_is_told_it_needs_no_approval(self):
        """The view writes nothing for a row the curator drew, so the
        line must not claim an approval: the box reads 1.0 from birth."""
        scan, det = self._make_scan_with_detection(
            model_name=Detection.ModelName.MANUAL, found_by=[], confidence=1.0
        )

        response = self._post(
            "approve_detection", scan, {"detection_id": det.pk}
        )

        self.assertEqual(
            response.json()["message"], views_api.OWN_DETECTION_MESSAGE
        )
        self.assertFalse(DetectionDecision.objects.exists())

    def test_a_new_anchor_box_names_the_recompute_and_a_plain_one_does_not(
        self,
    ):
        scan, _ = self._make_scan_with_detection()
        body = {
            "page_index": 1,
            "bbox": [500, 500, 600, 600],
            "img_width": 1200,
            "img_height": 1600,
        }

        response = self._post(
            "add_single_detection", scan, {**body, "label_id": 3}
        )

        self.assertEqual(
            response.json()["message"],
            views_api.ADDED_ANCHOR_DETECTION_MESSAGE,
        )

        response = self._post(
            "add_single_detection", scan, {**body, "label_id": 5}
        )

        self.assertEqual(
            response.json()["message"], views_api.ADDED_DETECTION_MESSAGE
        )

    def test_a_box_drawn_over_a_model_box_reads_as_an_approval(self):
        scan, det = self._make_scan_with_detection()

        response = self._post(
            "add_single_detection",
            scan,
            {
                "page_index": det.page_index,
                "label_id": det.label_id,
                "bbox": [det.x0, det.y0, det.x1, det.y1],
                "img_width": det.img_width,
                "img_height": det.img_height,
            },
        )

        self.assertFalse(response.json()["added"])
        self.assertEqual(
            response.json()["message"], views_api.APPROVED_DETECTION_MESSAGE
        )
        # The approval is a move by zero (#414): the answer names the
        # hand-drawn row, and the viewer's entry follows it.
        self.assertEqual(response.json()["replaced_id"], det.pk)
        self.assertNotEqual(response.json()["detection_id"], det.pk)


class TestBoundaryMessages(ScanningTestCase):
    """The lines of the three boundary writes."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.scan = make_scan()
        self.caption = make_detection(self.scan, "CASE_CAPTION", 0)
        self.key = make_detection(self.scan, "KEY_ICON", 3)
        self.boundary = make_boundary(self.scan, self.caption, self.key)

    def _post(self, name, body):
        return self.client.post(
            reverse(name, kwargs={"pk": self.scan.pk}),
            data=json.dumps(body),
            content_type="application/json",
        )

    def test_a_computed_boundary_is_dismissed(self):
        response = self._post(
            "dismiss_boundary", {"boundary_id": self.boundary.pk}
        )

        self.assertEqual(
            response.json()["message"], views_api.DISMISSED_BOUNDARY_MESSAGE
        )

    def test_the_restore_says_whether_a_dismissal_stood(self):
        self._post("dismiss_boundary", {"boundary_id": self.boundary.pk})

        self.assertEqual(
            self._post(
                "restore_boundary", {"boundary_id": self.boundary.pk}
            ).json()["message"],
            views_api.RESTORED_BOUNDARY_MESSAGE,
        )
        self.assertEqual(
            self._post(
                "restore_boundary", {"boundary_id": self.boundary.pk}
            ).json()["message"],
            views_api.STANDING_BOUNDARY_MESSAGE,
        )

    def test_an_added_boundary_says_added_and_a_replacing_one_moved(self):
        caption = make_detection(self.scan, "CASE_CAPTION", 1)
        key = make_detection(self.scan, "KEY_ICON", 2)
        anchors = {
            "start": {"detection_id": caption.pk},
            "end": {"detection_id": key.pk},
        }

        response = self._post("add_boundary", anchors)

        self.assertEqual(
            response.json()["message"], views_api.ADDED_BOUNDARY_MESSAGE
        )

        response = self._post(
            "add_boundary", {**anchors, "replaces": self.boundary.pk}
        )

        self.assertEqual(
            response.json()["message"], views_api.MOVED_BOUNDARY_MESSAGE
        )

    def test_a_withdrawn_addition_says_withdrawn(self):
        caption = make_detection(self.scan, "CASE_CAPTION", 1)
        key = make_detection(self.scan, "KEY_ICON", 2)
        added = self._post(
            "add_boundary",
            {
                "start": {"detection_id": caption.pk},
                "end": {"detection_id": key.pk},
            },
        ).json()["boundary_id"]

        response = self._post("dismiss_boundary", {"boundary_id": added})

        self.assertEqual(
            response.json()["message"], views_api.WITHDRAWN_BOUNDARY_MESSAGE
        )
        self.assertEqual(
            OpinionBoundary.objects.get(pk=added).origin,
            OpinionBoundary.Origin.HUMAN,
        )


class TestFindingMessages(ScanningTestCase):
    """The lines of the three findings writes and the rebuild."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.scan = make_scan()
        caption = make_detection(self.scan, "CASE_CAPTION", 0)
        key = make_detection(self.scan, "KEY_ICON", 3)
        make_boundary(self.scan, caption, key)
        make_detection(self.scan, "KEY_ICON", 1)
        findings.rebuild(self.scan)
        self.finding = self.scan.issues.first()

    def _post(self, name, body=None):
        return self.client.post(
            reverse(name, kwargs={"pk": self.scan.pk}),
            data=json.dumps(body or {}),
            content_type="application/json",
        )

    def test_the_dismiss_names_the_undo_on_the_card(self):
        response = self._post("dismiss_finding", {"issue_id": self.finding.pk})

        self.assertEqual(
            response.json()["message"], views_api.DISMISSED_FINDING_MESSAGE
        )
        self.assertTrue(ReviewDismissal.objects.exists())

    def test_the_restore_says_whether_a_dismissal_stood(self):
        self._post("dismiss_finding", {"issue_id": self.finding.pk})

        self.assertEqual(
            self._post(
                "restore_finding", {"issue_id": self.finding.pk}
            ).json()["message"],
            views_api.RESTORED_FINDING_MESSAGE,
        )
        self.assertEqual(
            self._post(
                "restore_finding", {"issue_id": self.finding.pk}
            ).json()["message"],
            views_api.STANDING_FINDING_MESSAGE,
        )

    def test_the_rebuild_says_it_read_the_rows(self):
        response = self._post("rebuild_findings")

        self.assertEqual(
            response.json()["message"], views_api.REBUILT_FINDINGS_MESSAGE
        )
