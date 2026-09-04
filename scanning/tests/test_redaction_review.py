"""Tests for the two redaction review states (issue #263).

Three groups, one per part of the design:

- the rule (:func:`review_states.redaction_review_ready`) and the pass
  that reads it on the collect tick;
- the redaction apply, which parks a scan it just finished in the new
  READY status itself, so the button is on the page the viewer reloads;
- the approve button, the only writer of ``REDACTION_REVIEW_DONE``, and
  the step-3 gate it opens.
"""

from unittest.mock import patch

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from scanning import review_states, services, yolo
from scanning.factories import ScanFactory
from scanning.models import (
    Detection,
    ExternalJob,
    JobStatus,
    Scan,
    Status,
)
from scanning.tests.test_jobs import make_manifest
from scanning.tests.test_views import ScanningTestCase
from scanning.tests.test_yolo_apply import ComputeMixin, merged_scan
from scanning.views_process import (
    REDACTION_REVIEW_ALREADY_DONE_MESSAGE,
    REDACTION_REVIEW_APPROVED_MESSAGE,
    REDACTION_REVIEW_NOT_READY_MESSAGE,
)


def applied_scan(status=Status.PAGE_COMPLETENESS_REVIEW_DONE, **kwargs):
    """Build a scan whose detection run is merged, consumed and applied.

    That is every condition of review 2 that the database can hold.

    :param status: The status to give the scan.
    :param kwargs: Extra fields for the factory.
    :returns: ``(scan, rows)``.
    """
    scan, rows = merged_scan(status=status, **kwargs)
    yolo.record_apply_success(rows)
    return scan, yolo.live_detect_jobs(scan)


class TestRedactionReviewReady(TestCase):
    """The rule behind the READY status."""

    def test_an_applied_run_is_ready(self):
        scan, rows = applied_scan()

        self.assertTrue(review_states.redaction_review_ready(scan, rows))

    def test_the_rows_are_read_when_the_caller_has_none(self):
        scan, _ = applied_scan()

        self.assertTrue(review_states.redaction_review_ready(scan))

    def test_a_run_nobody_applied_is_not_ready(self):
        """The geometry is what review 2 examines, and the stamp is
        what says it was measured."""
        scan, rows = merged_scan()

        self.assertFalse(review_states.redaction_review_ready(scan, rows))

    def test_a_run_that_is_not_merged_is_not_ready(self):
        scan, rows = applied_scan()
        ExternalJob.objects.filter(pk=rows[1].pk).update(
            status=JobStatus.COMPLETED
        )

        self.assertFalse(review_states.redaction_review_ready(scan))

    def test_a_volume_with_no_detection_run_is_not_ready(self):
        """A legacy volume: its step 2 lives in PENDING_REVIEW."""
        scan = ScanFactory(status=Status.PAGE_COMPLETENESS_REVIEW_DONE)

        self.assertFalse(review_states.redaction_review_ready(scan))

    def test_the_corrected_volume_is_a_condition(self):
        """The #224 hook. It says yes for every scan today, and the
        rule must ask it rather than assume it."""
        scan, rows = applied_scan()

        with patch.object(
            review_states, "final_volume_ready", return_value=False
        ):
            self.assertFalse(review_states.redaction_review_ready(scan, rows))


class TestPromoteReadyScans(TestCase):
    """The collect tick's pass, the safety net of the two writers."""

    def test_an_applied_volume_is_promoted(self):
        scan, _ = applied_scan()

        self.assertEqual(review_states.promote_ready_scans(), 1)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.READY_FOR_REDACTION_REVIEW)

    def test_a_promoted_volume_is_not_promoted_twice(self):
        applied_scan()

        self.assertEqual(review_states.promote_ready_scans(), 1)
        self.assertEqual(review_states.promote_ready_scans(), 0)

    def test_a_volume_with_no_geometry_waits(self):
        scan, _ = merged_scan()

        self.assertEqual(review_states.promote_ready_scans(), 0)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.PAGE_COMPLETENESS_REVIEW_DONE)

    def test_a_volume_review_one_has_not_approved_waits(self):
        """Review 2 follows review 1, and the pass never overtakes it."""
        scan, _ = applied_scan(
            status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )

        self.assertEqual(review_states.promote_ready_scans(), 0)

        scan.refresh_from_db()
        self.assertEqual(
            scan.status, Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )

    def test_a_volume_without_its_corrected_build_waits(self):
        """The other order the pass exists for (#224): the build lands
        after the geometry, and the apply is long over."""
        scan, _ = applied_scan()

        with patch.object(
            review_states, "final_volume_ready", return_value=False
        ):
            self.assertEqual(review_states.promote_ready_scans(), 0)

        self.assertEqual(review_states.promote_ready_scans(), 1)

    def test_an_approved_review_two_is_left_alone(self):
        scan, _ = applied_scan(status=Status.REDACTION_REVIEW_DONE)

        self.assertEqual(review_states.promote_ready_scans(), 0)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.REDACTION_REVIEW_DONE)


class TestTheApplyOpensReviewTwo(ComputeMixin, TestCase):
    """Where ``run_compute_redactions`` leaves the scan (#263).

    The daemon claims the scan before it runs the work, so every test
    here puts it in PROCESSING first: the park writes over the busy
    statuses alone.
    """

    def claim(self, scan):
        """Put a scan where ``process_next_scan`` puts it.

        :param scan: The scan to claim.
        :return: None.
        """
        Scan.objects.filter(pk=scan.pk).update(status=Status.PROCESSING)

    def test_a_successful_apply_parks_in_review_two(self):
        scan, _ = merged_scan()
        self.patch_geometry()
        self.claim(scan)

        services.run_compute_redactions(scan.pk)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.READY_FOR_REDACTION_REVIEW)

    def test_the_stamp_is_written_before_the_edge_is_read(self):
        """The rule reads ``applied_at``, so the other order would park
        a finished volume one tick short of its own review."""
        scan, _ = merged_scan()
        self.patch_geometry()
        self.claim(scan)

        services.run_compute_redactions(scan.pk)

        state = yolo.apply_state(yolo.live_detect_jobs(scan))
        self.assertTrue(state.get("applied_at"))

    def test_a_failed_apply_parks_in_review_one(self):
        """A curator must not be sent to judge geometry nobody
        measured."""
        scan, _ = merged_scan()
        stubs = self.patch_geometry()
        stubs["_compute_and_save_redaction_rects"].side_effect = RuntimeError(
            "no"
        )
        self.claim(scan)

        services.run_compute_redactions(scan.pk)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.PAGE_COMPLETENESS_REVIEW_DONE)

    def test_a_legacy_volume_goes_back_to_pending_review(self):
        """Its step 2 lives there: the #154 and #263 states describe a
        flow it never went through."""
        scan = ScanFactory(page_count=2, status=Status.PENDING_REVIEW)
        Detection.objects.create(
            scan=scan,
            page_index=0,
            label="KEY_ICON",
            label_id=1,
            confidence=1.0,
            x0=1,
            y0=2,
            x1=3,
            y1=4,
            model_name=Detection.ModelName.MANUAL,
        )
        self.patch_geometry()
        self.claim(scan)

        services.run_compute_redactions(scan.pk)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.PENDING_REVIEW)

    def test_a_second_detection_run_reaches_its_apply(self):
        """A volume already in review 2 is in ``APPLY_STATUSES``: an
        operator's re-run must be measured too."""
        scan = ScanFactory(
            page_count=2, status=Status.READY_FOR_REDACTION_REVIEW
        )
        yolo.ensure_detect_jobs(scan, make_manifest(2, 1))
        ExternalJob.objects.filter(scan=scan).update(status=JobStatus.CONSUMED)

        with patch("scanning.s3_sync.s3_active", return_value=True):
            self.assertEqual(yolo.queue_ready_runs(), 1)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.QUEUED)


class TestApproveRedactionReview(ScanningTestCase):
    """The approve button of review 2."""

    def setUp(self):
        self.user = self.make_user(username="reviewer")
        self.client.force_login(self.user)

    def _approve(self, scan):
        """POST the approve button and return the flashed messages.

        :param scan: The scan to approve.
        :returns: The flashed message strings.
        :rtype: list[str]
        """
        response = self.client.post(
            reverse("approve_redaction_review", kwargs={"pk": scan.pk})
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=2",
        )
        scan.refresh_from_db()
        return [str(m) for m in get_messages(response.wsgi_request)]

    def test_a_ready_scan_is_approved(self):
        scan = ScanFactory(status=Status.READY_FOR_REDACTION_REVIEW)

        flashed = self._approve(scan)

        self.assertEqual(scan.status, Status.REDACTION_REVIEW_DONE)
        self.assertIn(REDACTION_REVIEW_APPROVED_MESSAGE, flashed)

    def test_every_logged_in_user_may_approve(self):
        """The rule of the review-1 button (#151)."""
        self.client.force_login(self.make_user(username="volunteer"))
        scan = ScanFactory(status=Status.READY_FOR_REDACTION_REVIEW)

        self._approve(scan)

        self.assertEqual(scan.status, Status.REDACTION_REVIEW_DONE)

    def test_a_second_press_says_so_and_changes_nothing(self):
        scan = ScanFactory(status=Status.REDACTION_REVIEW_DONE)

        flashed = self._approve(scan)

        self.assertEqual(scan.status, Status.REDACTION_REVIEW_DONE)
        self.assertIn(REDACTION_REVIEW_ALREADY_DONE_MESSAGE, flashed)

    def test_a_scan_that_is_not_ready_is_refused(self):
        for status in (
            Status.PAGE_COMPLETENESS_REVIEW_DONE,
            Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
            Status.QUEUED,
            Status.ERROR,
        ):
            with self.subTest(status=status):
                scan = ScanFactory(status=status)

                flashed = self._approve(scan)

                self.assertEqual(scan.status, status)
                self.assertIn(REDACTION_REVIEW_NOT_READY_MESSAGE, flashed)

    def test_a_get_is_refused(self):
        scan = ScanFactory(status=Status.READY_FOR_REDACTION_REVIEW)

        response = self.client.get(
            reverse("approve_redaction_review", kwargs={"pk": scan.pk})
        )

        self.assertEqual(response.status_code, 405)
        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.READY_FOR_REDACTION_REVIEW)

    def test_a_logged_out_visitor_is_sent_to_the_login_page(self):
        self.client.logout()
        scan = ScanFactory(status=Status.READY_FOR_REDACTION_REVIEW)

        response = self.client.post(
            reverse("approve_redaction_review", kwargs={"pk": scan.pk})
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])
        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.READY_FOR_REDACTION_REVIEW)


class TestTheStepTwoBar(ScanningTestCase):
    """What the bar offers, and what it gates."""

    def setUp(self):
        self.user = self.make_user(username="reviewer")
        self.client.force_login(self.user)

    def _bar(self, scan):
        """Render the step-2 action bar.

        :param scan: The scan to render it for.
        :returns: The rendered HTML.
        :rtype: str
        """
        response = self.client.get(
            reverse("process_actions", kwargs={"pk": scan.pk}),
            {"step": 2},
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["html"]

    def test_the_approve_button_shows_only_when_the_scan_is_ready(self):
        ready = ScanFactory(status=Status.READY_FOR_REDACTION_REVIEW)
        waiting = ScanFactory(status=Status.PAGE_COMPLETENESS_REVIEW_DONE)

        self.assertIn("approve-redactions", self._bar(ready))
        self.assertNotIn("approve-redactions", self._bar(waiting))

    def test_an_approved_scan_shows_the_mark(self):
        scan = ScanFactory(status=Status.REDACTION_REVIEW_DONE)

        bar = self._bar(scan)

        self.assertIn("Redaction review done", bar)
        self.assertNotIn("approve-redactions", bar)

    def test_the_approval_is_the_gate_of_step_three(self):
        opinions = [{"id": 1}]
        waiting = ScanFactory(
            status=Status.READY_FOR_REDACTION_REVIEW,
            opinions_json=opinions,
        )
        approved = ScanFactory(
            status=Status.REDACTION_REVIEW_DONE, opinions_json=opinions
        )

        self.assertNotIn("Next: Generate", self._bar(waiting))
        self.assertIn("Next: Generate", self._bar(approved))

    def test_a_legacy_volume_keeps_its_link(self):
        """It can hold neither #263 status, so a gate on the approval
        alone would strand it in step 2."""
        scan = ScanFactory(
            status=Status.PENDING_REVIEW,
            opinions_json=[{"id": 1}],
            page_count=2,
        )

        with patch.object(services, "has_legacy_ocr", return_value=True):
            bar = self._bar(scan)

        self.assertIn("Next: Generate", bar)


class TestTheReviewStatesAreKept(ScanningTestCase):
    """What must not move a scan out of review 2."""

    def test_a_recompute_of_the_page_issues_keeps_the_status(self):
        """The recompute button of step 1 is reachable from a volume
        already in review 2, and it says nothing about the
        redactions."""
        for status in (
            Status.READY_FOR_REDACTION_REVIEW,
            Status.REDACTION_REVIEW_DONE,
        ):
            with self.subTest(status=status):
                scan = ScanFactory(status=status, page_count=2)

                services.recalculate_issues(scan)

                scan.refresh_from_db()
                self.assertEqual(scan.status, status)

    def test_the_preview_answer_names_a_reload(self):
        """Both states come after the bitonal merge, so a missing
        preview is a failed pull and not a missing file."""
        self.client.force_login(self.make_user(username="reviewer"))
        for status in (
            Status.READY_FOR_REDACTION_REVIEW,
            Status.REDACTION_REVIEW_DONE,
        ):
            with self.subTest(status=status):
                scan = ScanFactory(status=status)

                response = self.client.get(
                    reverse("serve_scan_pdf", kwargs={"pk": scan.pk})
                )

                self.assertEqual(response.status_code, 409)
                self.assertIn("Reload the page", response.json()["message"])
