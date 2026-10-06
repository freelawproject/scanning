"""Tests for the reopen of the redaction review (#240).

Five groups, one per part of the change:

- the staff reopen, a compare-and-swap from ``REDACTION_REVIEW_DONE``,
  and what stops while the review is open again;
- the write gate of review 2, which refuses after the approval;
- the rule ``detections.changed_since_compute``;
- the approval that computes first, and the chain from the compute to
  the opinions;
- the text approval of review 3, which waits for review 2.
"""

import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from scanning import (
    apply,
    detections,
    opinion_ocr,
    opinion_pdf,
    opinion_review,
    opinions,
    services,
    yolo,
)
from scanning.factories import OpinionFactory, ScanFactory, UserFactory
from scanning.models import (
    Detection,
    DetectionDecision,
    OpinionReviewStatus,
    QueuedAction,
    Scan,
    Status,
)
from scanning.tests.test_detection_preview import TestEveryWriteRefuses
from scanning.tests.test_opinion_approval import ApprovalTestCase
from scanning.tests.test_views import ScanningTestCase
from scanning.tests.test_yolo_apply import ComputeMixin, merged_scan
from scanning.views_api import REVIEW_APPROVED_MESSAGE
from scanning.views_process import (
    REDACTION_REVIEW_APPROVED_MESSAGE,
    REDACTION_REVIEW_COMPUTE_FIRST_MESSAGE,
    REDACTION_REVIEW_NOT_REOPENABLE_MESSAGE,
    REDACTION_REVIEW_QUEUED_MESSAGE,
    REDACTION_REVIEW_REOPENED_MESSAGE,
)


def computed_scan(status=Status.READY_FOR_REDACTION_REVIEW, read_at=None):
    """Build a scan whose redactions were computed, read at ``read_at``.

    :param status: The status to give the scan.
    :param read_at: When the compute read the rows; an hour ago when
        omitted, so that a row written in the test is newer.
    :returns: ``(scan, rows)``.
    """
    scan, rows = merged_scan(status=status)
    if read_at is None:
        read_at = timezone.now() - timedelta(hours=1)
    yolo.record_apply_success(rows, apply.current_run(scan), read_at=read_at)
    return scan, yolo.live_detect_jobs(scan)


def manual_detection(scan, **fields) -> Detection:
    """Write a hand-drawn detection row.

    :param scan: The scan.
    :param fields: Fields to set over the defaults.
    :returns: The row.
    """
    values = {
        "scan": scan,
        "page_index": 0,
        "label": "EDITORIAL",
        "label_id": 1,
        "confidence": 1.0,
        "x0": 10,
        "y0": 10,
        "x1": 100,
        "y1": 50,
        "model_name": Detection.ModelName.MANUAL,
    }
    values.update(fields)
    return Detection.objects.create(**values)


def decision(scan, **fields) -> DetectionDecision:
    """Write a dismissal of a model box.

    :param scan: The scan.
    :param fields: Fields to set over the defaults.
    :returns: The row.
    """
    values = {
        "scan": scan,
        "kind": DetectionDecision.Kind.DEACTIVATE,
        "source_page": 1,
        "label": "HEADNOTE",
        "label_id": 2,
        "target_x0": 10,
        "target_y0": 10,
        "target_x1": 100,
        "target_y1": 50,
    }
    values.update(fields)
    return DetectionDecision.objects.create(**values)


def an_hour_ago():
    """Return a time before the stamp of :func:`computed_scan`'s default.

    :returns: Two hours ago.
    """
    return timezone.now() - timedelta(hours=2)


class TestReopenRedactionReview(ScanningTestCase):
    """The staff button."""

    def setUp(self):
        self.staff = self.make_staff_user(username="staff")
        self.client.force_login(self.staff)

    def _reopen(self, scan):
        """POST the reopen button and return the flashed messages.

        :param scan: The scan to reopen.
        :returns: The flashed message strings.
        :rtype: list[str]
        """
        response = self.client.post(
            reverse("reopen_redaction_review", kwargs={"pk": scan.pk})
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=2",
        )
        scan.refresh_from_db()
        return [str(m) for m in get_messages(response.wsgi_request)]

    def test_an_approved_review_is_opened_again(self):
        scan = ScanFactory(status=Status.REDACTION_REVIEW_DONE)

        flashed = self._reopen(scan)

        self.assertEqual(scan.status, Status.READY_FOR_REDACTION_REVIEW)
        self.assertIn(REDACTION_REVIEW_REOPENED_MESSAGE, flashed)

    def test_the_opinions_stay(self):
        """The next approval matches them by key and keeps every human
        field; the reopen writes no opinion row."""
        scan = ScanFactory(status=Status.REDACTION_REVIEW_DONE)
        opinion = OpinionFactory(
            scan=scan, status=OpinionReviewStatus.TEXT_REVIEW_DONE
        )

        self._reopen(scan)

        opinion.refresh_from_db()
        self.assertEqual(opinion.status, OpinionReviewStatus.TEXT_REVIEW_DONE)

    def test_a_curator_who_is_not_staff_is_refused(self):
        self.client.force_login(self.make_user(username="volunteer"))
        scan = ScanFactory(status=Status.REDACTION_REVIEW_DONE)

        self._reopen(scan)

        self.assertEqual(scan.status, Status.REDACTION_REVIEW_DONE)

    def test_a_review_that_is_not_approved_is_refused(self):
        """The creation of the opinions in flight among them: the
        worker of the approval is never cut off."""
        for status, action in (
            (Status.READY_FOR_REDACTION_REVIEW, ""),
            (Status.QUEUED, QueuedAction.CREATE_OPINIONS),
            (Status.PROCESSING, QueuedAction.CREATE_OPINIONS),
            (Status.PAGE_COMPLETENESS_REVIEW_DONE, ""),
            (Status.ERROR, ""),
        ):
            with self.subTest(status=status):
                scan = ScanFactory(status=status, queued_action=action)

                flashed = self._reopen(scan)

                self.assertEqual(scan.status, status)
                self.assertIn(REDACTION_REVIEW_NOT_REOPENABLE_MESSAGE, flashed)

    def test_a_get_is_refused(self):
        scan = ScanFactory(status=Status.REDACTION_REVIEW_DONE)

        response = self.client.get(
            reverse("reopen_redaction_review", kwargs={"pk": scan.pk})
        )

        self.assertEqual(response.status_code, 405)

    def test_the_opinion_passes_stop_for_the_volume(self):
        """They read ``REDACTION_REVIEW_DONE`` alone, so a status write
        is the whole stop."""
        scan = ScanFactory(status=Status.REDACTION_REVIEW_DONE)
        opinion = OpinionFactory(scan=scan, glue_revision=1)
        self.assertIn(opinion, opinion_pdf.owed())
        self.assertIn(opinion, opinion_ocr.due())

        self._reopen(scan)

        self.assertNotIn(opinion, opinion_pdf.owed())
        self.assertNotIn(opinion, opinion_ocr.due())


class TestTheStepTwoBar(ScanningTestCase):
    """What the bar offers around the reopen."""

    def setUp(self):
        self.staff = self.make_staff_user(username="staff")
        self.client.force_login(self.staff)

    def _bar(self, scan):
        response = self.client.get(
            reverse("process_actions", kwargs={"pk": scan.pk}),
            {"step": 2},
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["html"]

    def test_the_button_shows_for_staff_on_an_approved_review(self):
        scan = ScanFactory(status=Status.REDACTION_REVIEW_DONE)

        self.assertIn("reopen-redactions", self._bar(scan))

    def test_the_button_is_hidden_from_a_curator(self):
        self.client.force_login(self.make_user(username="volunteer"))
        scan = ScanFactory(status=Status.REDACTION_REVIEW_DONE)

        self.assertNotIn("reopen-redactions", self._bar(scan))

    def test_the_button_is_hidden_on_an_open_review(self):
        scan = ScanFactory(status=Status.READY_FOR_REDACTION_REVIEW)

        self.assertNotIn("reopen-redactions", self._bar(scan))

    def test_the_confirm_names_the_approved_opinions(self):
        scan = ScanFactory(status=Status.REDACTION_REVIEW_DONE)
        OpinionFactory(scan=scan, status=OpinionReviewStatus.TEXT_REVIEW_DONE)

        self.assertIn("1 opinion of this volume has", self._bar(scan))

    def test_an_open_review_links_the_approved_opinions(self):
        scan = ScanFactory(status=Status.READY_FOR_REDACTION_REVIEW)
        approved = OpinionFactory(
            scan=scan, status=OpinionReviewStatus.TEXT_REVIEW_DONE
        )
        OpinionFactory(scan=scan, first_printed_page=9)

        bar = self._bar(scan)

        self.assertIn("review2-approved-opinions", bar)
        self.assertIn(
            reverse("opinion_review", kwargs={"pk": approved.pk}), bar
        )
        self.assertIn("1 approved opinion keeps", bar)

    def test_an_open_review_with_no_approved_opinion_shows_no_list(self):
        scan = ScanFactory(status=Status.READY_FOR_REDACTION_REVIEW)
        OpinionFactory(scan=scan)

        self.assertNotIn("review2-approved-opinions", self._bar(scan))

    def test_the_bar_says_the_approval_computes_first(self):
        scan, _ = computed_scan()
        self.assertNotIn("review2-compute-owed", self._bar(scan))

        manual_detection(scan)

        self.assertIn("review2-compute-owed", self._bar(scan))


class TestTheApprovedReviewRefusesWrites(ScanningTestCase):
    """A write after the approval reaches some opinions and not others,
    so the gate refuses it, and the reopen is the way back."""

    def setUp(self):
        self.client.force_login(self.make_user(username="reviewer"))
        self.scan = ScanFactory(status=Status.REDACTION_REVIEW_DONE)

    def test_every_write_of_step_two_answers_409(self):
        """The findings rebuild aside: it writes the cards again from
        the rows and reaches no opinion (#305)."""
        for name, body in TestEveryWriteRefuses.WRITES:
            if name == "rebuild_findings":
                continue
            with self.subTest(endpoint=name):
                response = self.client.post(
                    reverse(name, kwargs={"pk": self.scan.pk}),
                    data=json.dumps(body),
                    content_type="application/json",
                )

                self.assertEqual(response.status_code, 409)
                self.assertEqual(
                    response.json()["message"], REVIEW_APPROVED_MESSAGE
                )

    def test_the_per_row_writes_answer_409(self):
        for name in TestEveryWriteRefuses.ROW_WRITES:
            with self.subTest(endpoint=name):
                response = self.client.post(
                    reverse(
                        name, kwargs={"pk": self.scan.pk, "redaction_id": 1}
                    ),
                    data=json.dumps({"x0": 1, "y0": 2, "x1": 3, "y1": 4}),
                    content_type="application/json",
                )

                self.assertEqual(response.status_code, 409)

    def test_a_reopened_review_takes_the_write(self):
        Scan.objects.filter(pk=self.scan.pk).update(
            status=Status.READY_FOR_REDACTION_REVIEW
        )

        response = self.client.post(
            reverse("rebuild_findings", kwargs={"pk": self.scan.pk}),
            data="{}",
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)


class TestChangedSinceCompute(TestCase):
    """The one rule of "the approval owes a compute"."""

    def test_no_human_row_owes_nothing(self):
        scan, _ = computed_scan()

        self.assertFalse(detections.changed_since_compute(scan))

    def test_a_volume_with_no_detection_run_owes_nothing(self):
        scan = ScanFactory(status=Status.READY_FOR_REDACTION_REVIEW)
        manual_detection(scan)

        self.assertFalse(detections.changed_since_compute(scan))

    def test_a_box_drawn_after_the_read_is_a_change(self):
        scan, _ = computed_scan()
        manual_detection(scan)

        self.assertTrue(detections.changed_since_compute(scan))

    def test_a_box_drawn_before_the_read_is_not(self):
        scan, _ = computed_scan()
        row = manual_detection(scan)
        Detection.objects.filter(pk=row.pk).update(date_created=an_hour_ago())

        self.assertFalse(detections.changed_since_compute(scan))

    def test_a_box_withdrawn_after_the_read_is_a_change(self):
        """The withdrawal is an ``update()``, which leaves
        ``date_modified`` alone, so the rule reads ``withdrawn_at``."""
        scan, _ = computed_scan()
        row = manual_detection(scan)
        Detection.objects.filter(pk=row.pk).update(date_created=an_hour_ago())

        self.assertTrue(detections.withdraw_manual(row, None))

        self.assertTrue(detections.changed_since_compute(scan))

    def test_a_decision_written_after_the_read_is_a_change(self):
        """A dismissal of a model box changes the live rows too."""
        scan, _ = computed_scan()
        decision(scan)

        self.assertTrue(detections.changed_since_compute(scan))

    def test_a_decision_withdrawn_after_the_read_is_a_change(self):
        scan, _ = computed_scan()
        row = decision(scan)
        DetectionDecision.objects.filter(pk=row.pk).update(
            date_created=an_hour_ago()
        )
        self.assertFalse(detections.changed_since_compute(scan))

        DetectionDecision.objects.filter(pk=row.pk).update(
            withdrawn_at=timezone.now()
        )

        self.assertTrue(detections.changed_since_compute(scan))

    def test_a_model_row_is_not_a_change(self):
        """The compute writes the model rows itself."""
        scan, _ = computed_scan()
        manual_detection(scan, model_name=Detection.ModelName.BL_WARM)

        self.assertFalse(detections.changed_since_compute(scan))

    def test_a_compute_before_the_stamp_counts_every_human_row(self):
        """One compute too many, never a change missed."""
        scan, rows = merged_scan(status=Status.READY_FOR_REDACTION_REVIEW)
        yolo.record_apply_success(rows, apply.current_run(scan))
        self.assertFalse(detections.changed_since_compute(scan))
        row = manual_detection(scan)
        Detection.objects.filter(pk=row.pk).update(date_created=an_hour_ago())

        self.assertTrue(detections.changed_since_compute(scan))


class TestTheApprovalComputesFirst(ScanningTestCase):
    """The approve button picks the action from the rule."""

    def setUp(self):
        self.client.force_login(self.make_user(username="reviewer"))

    def _approve(self, scan):
        response = self.client.post(
            reverse("approve_redaction_review", kwargs={"pk": scan.pk})
        )
        scan.refresh_from_db()
        return [str(m) for m in get_messages(response.wsgi_request)]

    def test_an_unchanged_volume_creates_the_opinions(self):
        scan, _ = computed_scan()

        flashed = self._approve(scan)

        self.assertEqual(scan.status, Status.QUEUED)
        self.assertEqual(scan.queued_action, QueuedAction.CREATE_OPINIONS)
        self.assertIn(REDACTION_REVIEW_APPROVED_MESSAGE, flashed)

    def test_a_changed_volume_computes_first(self):
        scan, _ = computed_scan()
        manual_detection(scan)

        flashed = self._approve(scan)

        self.assertEqual(scan.status, Status.QUEUED)
        self.assertEqual(
            scan.queued_action, QueuedAction.COMPUTE_THEN_CREATE_OPINIONS
        )
        self.assertIn(REDACTION_REVIEW_COMPUTE_FIRST_MESSAGE, flashed)

    def test_a_second_press_during_the_compute_says_so(self):
        scan = ScanFactory(
            status=Status.PROCESSING,
            queued_action=QueuedAction.COMPUTE_THEN_CREATE_OPINIONS,
        )

        flashed = self._approve(scan)

        self.assertEqual(scan.status, Status.PROCESSING)
        self.assertIn(REDACTION_REVIEW_QUEUED_MESSAGE, flashed)

    def test_the_action_is_dispatched(self):
        """A queued action with no worker parks in the legacy arm."""
        from scanning.management.commands import process_next_scan

        self.assertIn(
            QueuedAction.COMPUTE_THEN_CREATE_OPINIONS,
            process_next_scan.CLAIM_PRIORITY,
        )


class TestTheChain(ComputeMixin, TestCase):
    """The compute of an approval hands the scan to the opinions."""

    def claim(self, scan):
        Scan.objects.filter(pk=scan.pk).update(
            status=Status.PROCESSING,
            queued_action=QueuedAction.COMPUTE_THEN_CREATE_OPINIONS,
        )

    def test_a_successful_compute_queues_the_opinions(self):
        scan, _ = computed_scan()
        self.patch_geometry()
        self.claim(scan)

        services.run_compute_then_create_opinions(scan.pk)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.QUEUED)
        self.assertEqual(scan.queued_action, QueuedAction.CREATE_OPINIONS)

    def test_the_read_time_is_stamped_before_the_end(self):
        """``applied_at`` is the end of the compute, a render later."""
        scan, _ = merged_scan()
        self.patch_geometry()
        Scan.objects.filter(pk=scan.pk).update(status=Status.PROCESSING)
        before = timezone.now()

        services.run_compute_redactions(scan.pk)

        state = yolo.apply_state(yolo.live_detect_jobs(scan))
        read_at = timezone.datetime.fromisoformat(state["rows_read_at"])
        applied_at = timezone.datetime.fromisoformat(state["applied_at"])
        self.assertLessEqual(before, read_at)
        self.assertLessEqual(read_at, applied_at)

    def test_the_compute_answers_the_change(self):
        """The box the approval saw is in the geometry now, so a second
        approval creates the opinions alone."""
        scan, _ = computed_scan()
        manual_detection(scan)
        self.patch_geometry()
        self.claim(scan)

        services.run_compute_then_create_opinions(scan.pk)

        self.assertFalse(detections.changed_since_compute(scan))

    def test_a_failed_compute_parks_in_the_review(self):
        scan, _ = computed_scan()
        stubs = self.patch_geometry()
        stubs["_measure_redaction_rects"].side_effect = RuntimeError("no")
        self.claim(scan)

        services.run_compute_then_create_opinions(scan.pk)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.READY_FOR_REDACTION_REVIEW)
        self.assertIn(services.APPROVAL_NOT_DONE_NOTE, scan.progress_message)

    def test_a_plain_compute_does_not_chain(self):
        scan, _ = computed_scan()
        self.patch_geometry()
        Scan.objects.filter(pk=scan.pk).update(
            status=Status.PROCESSING,
            queued_action=QueuedAction.COMPUTE_REDACTIONS,
        )

        services.run_compute_redactions(scan.pk)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.READY_FOR_REDACTION_REVIEW)
        self.assertNotIn(
            services.APPROVAL_NOT_DONE_NOTE, scan.progress_message
        )

    def test_an_admin_move_during_the_compute_is_kept(self):
        """The hand-off is a compare-and-swap over the busy statuses."""
        scan, _ = computed_scan()
        self.patch_geometry()
        self.claim(scan)
        real = services._queue_opinions_after_compute

        def moved(scan_pk, open_findings):
            Scan.objects.filter(pk=scan_pk).update(status=Status.ERROR)
            real(scan_pk, open_findings)

        with patch.object(
            services, "_queue_opinions_after_compute", side_effect=moved
        ):
            services.run_compute_then_create_opinions(scan.pk)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.ERROR)


class TestQueueCreateOpinions(TestCase):
    def test_a_lost_write_answers_nothing(self):
        scan = ScanFactory(status=Status.REDACTION_REVIEW_DONE)

        self.assertEqual(opinions.queue_create_opinions(scan), "")


class TestTheTextWaitsForReviewTwo(ApprovalTestCase):
    """An approval while review 2 is open would keep the old
    redactions, because the next approval of review 2 does not build an
    approved text again."""

    def reopen(self):
        Scan.objects.filter(pk=self.opinion.scan_id).update(
            status=Status.READY_FOR_REDACTION_REVIEW
        )

    def test_the_gate_refuses(self):
        self.dismiss_blocking()
        self.reopen()

        with self.assertRaises(opinion_review.ApprovalRefused) as caught:
            self.approve()

        self.assertEqual(caught.exception.code, opinion_review.REVIEW2_OPEN)
        self.opinion.refresh_from_db()
        self.assertEqual(
            self.opinion.status, OpinionReviewStatus.READY_FOR_TEXT_REVIEW
        )

    def test_the_swap_refuses_a_reopen_after_the_gate(self):
        """The reopen can land between the gate and the swap."""
        self.dismiss_blocking()
        real = opinion_review.blocking_findings
        calls = []

        def reopen_at_the_swap(opinion):
            calls.append(opinion)
            if len(calls) == 2:
                self.reopen()
            return real(opinion)

        with (
            patch.object(
                opinion_review,
                "blocking_findings",
                side_effect=reopen_at_the_swap,
            ),
            self.assertRaises(opinion_review.ApprovalRefused) as caught,
        ):
            self.approve()

        self.assertEqual(caught.exception.code, opinion_review.MOVED)

    def test_the_page_offers_no_approval(self):
        self.dismiss_blocking()
        self.reopen()
        self.client.force_login(UserFactory())

        response = self.client.get(
            reverse("opinion_review", kwargs={"pk": self.opinion.pk})
        )

        self.assertFalse(response.context["can_approve"])
        self.assertContains(response, "review2-open-note")
