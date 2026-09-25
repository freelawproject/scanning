"""Tests for the approval of an opinion's text (issue #375).

A curator approves the text of one opinion once no blocking card is
open. The approval writes the approved text, the one object the final
XML and the tagger read, then moves the row to ``TEXT_REVIEW_DONE`` by
a compare-and-swap. Four groups here:

- the gate (``opinion_review.check_gate``, ``blocking_findings``);
- the write: the object first, the row second, the key, the lost swap;
- the endpoints and the page;
- the staff reopen and the rewrite under a new join rule.
"""

from io import StringIO
from unittest.mock import patch

from django.contrib import messages
from django.contrib.messages import get_messages
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse

from scanning import ensemble, opinion_findings, opinion_review, paragraphs
from scanning.factories import OpinionFindingFactory, ScanFactory
from scanning.models import (
    Issue,
    Opinion,
    OpinionCheck,
    OpinionReviewStatus,
)
from scanning.tests.test_opinion_edits import EditTestCase
from scanning.tests.test_opinion_ocr import PRINTED
from scanning.tests.test_views import ScanningTestCase


@override_settings(OPINION_ENSEMBLE_MIN_ENGINES=2)
class ApprovalTestCase(EditTestCase):
    """The #376 fixture: page 1 holds one block the engines split, so a
    ``NO_MAJORITY`` card (ERROR) is open until a curator answers it."""

    def blocking(self):
        return list(opinion_review.blocking_findings(self.opinion))

    def dismiss_blocking(self):
        for finding in self.blocking():
            opinion_findings.dismiss(self.opinion, finding, None)

    def approve(self, user=None, **revisions):
        self.opinion.refresh_from_db()
        return opinion_review.approve_text(self.opinion, user, **revisions)


class TestTheGate(ApprovalTestCase):
    def test_an_open_error_card_refuses(self):
        self.assertTrue(self.blocking())

        with self.assertRaises(opinion_review.ApprovalRefused) as caught:
            self.approve()

        self.assertEqual(caught.exception.code, opinion_review.BLOCKED)
        self.assertEqual(caught.exception.count, len(self.blocking()))
        self.opinion.refresh_from_db()
        self.assertEqual(
            self.opinion.status, OpinionReviewStatus.READY_FOR_TEXT_REVIEW
        )

    def test_a_dismissed_error_card_lets_it_pass(self):
        self.dismiss_blocking()

        self.approve()

        self.opinion.refresh_from_db()
        self.assertEqual(
            self.opinion.status, OpinionReviewStatus.TEXT_REVIEW_DONE
        )

    def test_an_open_warning_does_not_block(self):
        self.dismiss_blocking()
        OpinionFindingFactory(
            opinion=self.opinion,
            page_in_opinion=2,
            check_name=OpinionCheck.PARTIAL_REDACTION,
            severity=Issue.Severity.WARNING,
        )

        self.approve()

        self.opinion.refresh_from_db()
        self.assertEqual(
            self.opinion.status, OpinionReviewStatus.TEXT_REVIEW_DONE
        )

    def test_a_stale_card_blocks_although_it_is_a_warning(self):
        self.dismiss_blocking()
        OpinionFindingFactory(
            opinion=self.opinion,
            page_in_opinion=None,
            check_name=OpinionCheck.ORPHANED_OPINION,
            severity=Issue.Severity.WARNING,
        )

        with self.assertRaises(opinion_review.ApprovalRefused) as caught:
            self.approve()

        self.assertEqual(caught.exception.code, opinion_review.BLOCKED)

    def test_an_unresolved_edit_blocks(self):
        self.dismiss_blocking()
        OpinionFindingFactory(
            opinion=self.opinion,
            page_in_opinion=None,
            check_name=OpinionCheck.UNRESOLVED_EDIT,
            severity=Issue.Severity.ERROR,
        )

        with self.assertRaises(opinion_review.ApprovalRefused):
            self.approve()

    def test_an_edit_the_text_does_not_hold_refuses(self):
        self.dismiss_blocking()
        Opinion.objects.filter(pk=self.opinion.pk).update(edit_revision=9)

        with self.assertRaises(opinion_review.ApprovalRefused) as caught:
            self.approve()

        self.assertEqual(caught.exception.code, opinion_review.NOT_BUILT)

    def test_a_page_drawn_at_another_revision_refuses(self):
        self.dismiss_blocking()

        with self.assertRaises(opinion_review.ApprovalRefused) as caught:
            self.approve(glue_revision=self.opinion.glue_revision + 1)

        self.assertEqual(caught.exception.code, opinion_review.STALE_PAGE)

    def test_a_row_not_ready_refuses(self):
        self.dismiss_blocking()
        Opinion.objects.filter(pk=self.opinion.pk).update(
            status=OpinionReviewStatus.PROCESSING
        )

        with self.assertRaises(opinion_review.ApprovalRefused) as caught:
            self.approve()

        self.assertEqual(caught.exception.code, opinion_review.CLOSED)

    def test_a_document_older_than_schema_8_refuses(self):
        self.dismiss_blocking()
        self.stored()["schema_version"] = 7

        with self.assertRaises(opinion_review.ApprovalRefused) as caught:
            self.approve()

        self.assertEqual(caught.exception.code, opinion_review.OLD_DOCUMENT)

    def test_an_edited_block_leaves_no_card(self):
        """A text edit is the other answer to a blocking card (#376)."""
        from scanning.models import OpinionEdit

        group = self.split_group()
        self.write_edit(
            kind=OpinionEdit.Kind.TEXT,
            box_pt=group["box_pt"],
            base_text=group["text"],
            text="body A 1",
        )
        self.opinion.refresh_from_db()
        ensemble.rerun(self.opinion)

        self.assertEqual(self.blocking(), [])
        self.approve()


class TestTheWrite(ApprovalTestCase):
    def setUp(self):
        super().setUp()
        self.dismiss_blocking()

    def test_the_object_goes_up_and_the_row_names_it(self):
        user = ScanningTestCase.make_user(self)

        key = self.approve(user)

        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.approved_text_key, key)
        self.assertEqual(self.opinion.approved_by, user)
        self.assertIsNotNone(self.opinion.approved_at)
        self.assertIn("/approved/r", key)
        self.assertTrue(key.endswith(f".j{paragraphs.JOIN_RULE}.json"))
        self.assertNotIn(self.opinion.glue_prefix, key)
        text = self.uploads[key]
        self.assertEqual(text["schema"], paragraphs.APPROVED_SCHEMA)
        self.assertEqual(text["approved_by"], user.username)
        self.assertEqual(
            [page["printed"] for page in text["pages"]],
            [PRINTED] * self.opinion.page_count,
        )
        self.assertTrue(text["body"])

    def test_the_object_holds_no_dropped_text_and_no_geometry(self):
        key = self.approve()

        text = self.uploads[key]
        document = self.stored()
        dropped = {
            group["text"]
            for page in document["pages"]
            for group in page["groups"]
            if group["band"] != "body"
        }
        flow = " ".join(entry["text"] for entry in text["body"])
        for furniture in dropped:
            self.assertNotIn(furniture, flow)
        self.assertNotIn("box_pt", str(text))

    def test_the_document_names_the_place_of_every_drop(self):
        """Schema 8: the join rule reads ``after`` and ``band``."""
        document = self.stored()

        drops = [d for page in document["pages"] for d in page["dropped"]]
        self.assertTrue(drops)
        for entry in drops:
            self.assertIn("after", entry)
            self.assertIn(entry["band"], ("head", "body", "foot", "footnotes"))
            self.assertIn(
                entry["section"], (ensemble.BODY, ensemble.FOOTNOTES)
            )

    def test_a_lost_swap_deletes_the_object_and_keeps_the_row(self):
        self.opinion.refresh_from_db()
        # Another tab wrote an edit after this request read the row.
        Opinion.objects.filter(pk=self.opinion.pk).update(
            edit_revision=self.opinion.edit_revision + 1,
            ensemble_edit_revision=self.opinion.ensemble_edit_revision + 1,
        )

        with patch("scanning.s3_sync.delete_objects") as delete:
            with self.assertRaises(opinion_review.ApprovalRefused) as caught:
                opinion_review.approve_text(self.opinion, None)

        self.assertEqual(caught.exception.code, opinion_review.MOVED)
        key = opinion_review.approved_key(
            self.opinion, self.opinion.ensemble_edit_revision
        )
        delete.assert_called_once_with([key])
        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.approved_text_key, "")

    def test_a_card_written_during_the_write_refuses_the_swap(self):
        def rebuild(key, data):
            self.uploads[key] = data
            OpinionFindingFactory(
                opinion=self.opinion,
                page_in_opinion=0,
                check_name=OpinionCheck.SINGLE_ENGINE,
                severity=Issue.Severity.ERROR,
            )
            return True

        with (
            patch("scanning.s3_sync.upload_json_object", side_effect=rebuild),
            patch("scanning.s3_sync.delete_objects") as delete,
            self.assertRaises(opinion_review.ApprovalRefused) as caught,
        ):
            self.approve()

        self.assertEqual(caught.exception.code, opinion_review.BLOCKED)
        delete.assert_called_once()

    def test_a_second_approval_at_the_same_key_writes_nothing(self):
        self.opinion.refresh_from_db()
        key = opinion_review.approved_key(
            self.opinion, self.opinion.ensemble_edit_revision
        )
        self.objects[key] = {"schema": 1, "approved_by": "first"}

        self.assertEqual(self.approve(), key)

        self.assertNotIn(key, self.uploads)
        self.assertEqual(self.objects[key]["approved_by"], "first")

    def test_a_failed_upload_refuses_and_moves_nothing(self):
        with patch("scanning.s3_sync.upload_json_object", return_value=False):
            with self.assertRaises(opinion_review.ApprovalRefused) as caught:
                self.approve()

        self.assertEqual(caught.exception.code, opinion_review.BUCKET)
        self.opinion.refresh_from_db()
        self.assertEqual(
            self.opinion.status, OpinionReviewStatus.READY_FOR_TEXT_REVIEW
        )

    def test_no_run_is_no_page_numbers(self):
        Opinion.objects.filter(pk=self.opinion.pk).update(apply_run=None)

        with self.assertRaises(opinion_review.ApprovalRefused) as caught:
            self.approve()

        self.assertEqual(caught.exception.code, opinion_review.NO_PAGE_NUMBERS)

    def test_an_approved_row_is_not_built_again(self):
        self.approve()
        self.opinion.refresh_from_db()

        self.assertFalse(ensemble.due().filter(pk=self.opinion.pk).exists())


class TestTheRewrite(ApprovalTestCase):
    def setUp(self):
        super().setUp()
        self.dismiss_blocking()
        self.first = self.approve(ScanningTestCase.make_user(self))
        self.opinion.refresh_from_db()

    def test_the_same_rule_writes_nothing(self):
        self.assertIsNone(opinion_review.rewrite_text(self.opinion))

    def test_a_new_rule_writes_a_new_key_and_keeps_the_approval(self):
        approved_at = self.opinion.approved_at

        with patch.object(paragraphs, "JOIN_RULE", 2):
            key = opinion_review.approved_key(
                self.opinion, self.opinion.ensemble_edit_revision, 2
            )
            call_command("rewrite_approved_text", "--all", stdout=StringIO())

        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.approved_text_key, key)
        self.assertNotEqual(key, self.first)
        self.assertIn(self.first, self.objects)
        self.assertEqual(self.opinion.approved_at, approved_at)
        self.assertEqual(
            self.uploads[key]["approved_by"], self.opinion.approved_by.username
        )

    def test_the_dry_run_changes_nothing(self):
        with patch.object(paragraphs, "JOIN_RULE", 2):
            call_command(
                "rewrite_approved_text",
                "--all",
                "--dry-run",
                stdout=StringIO(),
            )

        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.approved_text_key, self.first)


class TestTheEndpoints(ApprovalTestCase, ScanningTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        self.client.force_login(self.user)

    def url(self, name, scan=None) -> str:
        return reverse(
            name,
            kwargs={
                "pk": (scan or self.scan).pk,
                "opinion_pk": self.opinion.pk,
            },
        )

    def post(self, name, scan=None, **body):
        document = self.stored()
        body.setdefault("glue_revision", document["opinion"]["glue_revision"])
        body.setdefault("edit_revision", document.get("edit_revision", 0))
        self.client.cookies.pop("messages", None)
        return self.client.post(
            self.url(name, scan), body, content_type="application/json"
        )

    def said(self, response):
        return [
            (message.level, message.message)
            for message in get_messages(response.wsgi_request)
        ]

    def test_a_blocked_approval_answers_the_count(self):
        response = self.post("approve_opinion_text")

        self.assertEqual(response.status_code, 409)
        count = len(self.blocking())
        self.assertIn(str(count), response.json()["message"])
        self.assertEqual(
            self.said(response), [(messages.ERROR, response.json()["message"])]
        )

    def test_an_approval_answers_a_message(self):
        self.dismiss_blocking()

        response = self.post("approve_opinion_text")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.said(response),
            [(messages.SUCCESS, response.json()["message"])],
        )
        self.opinion.refresh_from_db()
        self.assertEqual(self.opinion.approved_by, self.user)

    def test_an_opinion_of_another_scan_is_a_404(self):
        response = self.post("approve_opinion_text", scan=ScanFactory())

        self.assertEqual(response.status_code, 404)

    def test_the_reopen_is_for_staff_alone(self):
        self.dismiss_blocking()
        self.post("approve_opinion_text")

        response = self.post("reopen_opinion_text")

        self.assertEqual(response.status_code, 403)
        self.opinion.refresh_from_db()
        self.assertEqual(
            self.opinion.status, OpinionReviewStatus.TEXT_REVIEW_DONE
        )

    def test_a_staff_reopen_keeps_the_approved_text(self):
        self.dismiss_blocking()
        self.post("approve_opinion_text")
        self.opinion.refresh_from_db()
        key = self.opinion.approved_text_key
        self.client.force_login(self.make_staff_user())

        response = self.post("reopen_opinion_text")

        self.assertEqual(response.status_code, 200)
        self.opinion.refresh_from_db()
        self.assertEqual(
            self.opinion.status, OpinionReviewStatus.READY_FOR_TEXT_REVIEW
        )
        self.assertIsNone(self.opinion.approved_at)
        self.assertIsNone(self.opinion.approved_by)
        self.assertEqual(self.opinion.approved_text_key, key)

    def test_a_reopen_of_a_row_not_approved_refuses(self):
        self.client.force_login(self.make_staff_user())

        response = self.post("reopen_opinion_text")

        self.assertEqual(response.status_code, 409)


class TestThePage(ApprovalTestCase, ScanningTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.make_user())

    def page_context(self):
        return self.client.get(
            reverse("opinion_review", kwargs={"pk": self.opinion.pk})
        )

    def test_the_button_is_held_while_a_blocking_card_is_open(self):
        response = self.page_context()

        self.assertTrue(response.context["can_approve"])
        self.assertEqual(
            response.context["blocking_count"], len(self.blocking())
        )
        self.assertContains(response, 'id="approve-text"')
        self.assertContains(response, "blocking finding")
        self.assertContains(
            response, 'disabled title="Resolve the blocking findings first'
        )

    def test_the_button_is_open_once_the_cards_are_answered(self):
        self.dismiss_blocking()

        response = self.page_context()

        self.assertEqual(response.context["blocking_count"], 0)
        self.assertContains(response, "Approve the text")

    def test_an_approved_opinion_offers_no_approval_and_staff_a_reopen(self):
        self.dismiss_blocking()
        self.approve()
        self.client.force_login(self.make_staff_user())

        response = self.page_context()

        self.assertFalse(response.context["can_approve"])
        self.assertTrue(response.context["can_reopen"])
        self.assertNotContains(response, 'id="approve-text"')
        self.assertContains(response, 'id="reopen-text"')
