"""Tests for the page completeness review interface (issue #151).

Review 1 asks a person one question -- is this volume page complete? --
and #151 gives that question a button. Under test:

- the approve button: the only writer of
  ``PAGE_COMPLETENESS_REVIEW_DONE`` (#154), a compare-and-swap on READY
- the recompute button: what it does on a new-pipeline scan, and what
  it says instead on a scan the retired PaddleOCR stage read (#173)
- the step-1 button bar, which the page and the ``process_actions``
  fragment must render the same way
"""

from django.contrib.messages import get_messages
from django.urls import reverse

from scanning import dots_mocr, services
from scanning.factories import ExternalJobFactory, ScanFactory
from scanning.models import (
    Detection,
    JobEngine,
    JobStage,
    JobStatus,
    PageInsert,
    Status,
)
from scanning.tests.test_dots_mocr_apply import ApplyRunsMixin
from scanning.tests.test_views import ScanningTestCase
from scanning.views_process import (
    LEGACY_OCR_RECOMPUTE_MESSAGE,
    PAGE_REVIEW_ALREADY_DONE_MESSAGE,
    PAGE_REVIEW_APPROVED_MESSAGE,
    PAGE_REVIEW_NOT_READY_MESSAGE,
    PENDING_EDITS_SAVED_MESSAGE,
    RECOMPUTE_DONE_MESSAGE,
    REVALIDATE_UNAVAILABLE_MESSAGE,
)


def dots_results(count=2):
    """Build ``ocr_results`` of the shape the dots.mocr apply writes.

    :param count: How many pages to describe.
    :returns: One entry per page, each with a ``dots-`` zone.
    :rtype: list[dict]
    """
    return [
        {
            "pdf_page": page,
            "detected": str(page),
            "type": "single",
            "zone": "dots-header",
        }
        for page in range(1, count + 1)
    ]


def legacy_results(count=2):
    """Build ``ocr_results`` of the shape the retired stage left behind.

    :param count: How many pages to describe.
    :returns: One entry per page, none carrying a ``dots-`` zone.
    :rtype: list[dict]
    """
    return [
        {"pdf_page": page, "detected": str(page), "type": "single"}
        for page in range(1, count + 1)
    ]


class TestHasLegacyOcr(ScanningTestCase):
    """Which readings the recompute may act on."""

    def test_a_dots_zone_is_the_new_pipeline(self):
        scan = ScanFactory(ocr_results=dots_results())
        self.assertFalse(services.has_legacy_ocr(scan))

    def test_no_zone_and_no_run_is_the_retired_stage(self):
        scan = ScanFactory(ocr_results=legacy_results())
        self.assertTrue(services.has_legacy_ocr(scan))

    def test_an_analyze_row_answers_for_a_volume_dots_read_blank(self):
        """dots read every page and found no number anywhere, so no
        entry carries a zone. The run is what proves it ran."""
        scan = ScanFactory(ocr_results=legacy_results())
        ExternalJobFactory(
            scan=scan,
            stage=JobStage.ANALYZE,
            engine=JobEngine.DOTS_MOCR,
            status=JobStatus.CONSUMED,
        )
        self.assertFalse(services.has_legacy_ocr(scan))

    def test_a_scan_with_no_readings_is_not_legacy(self):
        """It has nothing to recompute either way, and the view
        answers that case before it asks this one."""
        self.assertFalse(services.has_legacy_ocr(ScanFactory()))


class TestApprovePageCompleteness(ScanningTestCase):
    """The approve button of review 1."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)

    def _approve(self, scan):
        """POST the approve button and return the flashed messages.

        :param scan: The scan to approve.
        :returns: The flashed message strings.
        :rtype: list[str]
        """
        response = self.client.post(
            reverse("approve_page_completeness", kwargs={"pk": scan.pk})
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=1",
        )
        scan.refresh_from_db()
        return [str(m) for m in get_messages(response.wsgi_request)]

    def test_a_ready_scan_is_approved(self):
        scan = ScanFactory(
            status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW, page_count=2
        )

        flashed = self._approve(scan)

        self.assertEqual(scan.status, Status.PAGE_COMPLETENESS_REVIEW_DONE)
        self.assertIn(PAGE_REVIEW_APPROVED_MESSAGE, flashed)

    def test_every_logged_in_user_may_approve(self):
        """Review 1 is the scanners' own step, not a staff one."""
        self.client.force_login(self.make_user(username="volunteer"))
        scan = ScanFactory(
            status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW, page_count=2
        )

        self._approve(scan)

        self.assertEqual(scan.status, Status.PAGE_COMPLETENESS_REVIEW_DONE)

    def test_open_issues_do_not_block_the_approval(self):
        """The curator is the judge of the model's suspicions. The
        browser asks for a confirm; the view obeys."""
        scan = ScanFactory(
            status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
            page_count=2,
            missing_pages=[2],
        )

        self._approve(scan)

        self.assertEqual(scan.status, Status.PAGE_COMPLETENESS_REVIEW_DONE)

    def test_a_second_approval_changes_nothing(self):
        scan = ScanFactory(
            status=Status.PAGE_COMPLETENESS_REVIEW_DONE, page_count=2
        )

        flashed = self._approve(scan)

        self.assertEqual(scan.status, Status.PAGE_COMPLETENESS_REVIEW_DONE)
        self.assertIn(PAGE_REVIEW_ALREADY_DONE_MESSAGE, flashed)

    def test_a_scan_that_is_not_ready_is_refused(self):
        """A stale page left open must not approve a volume whose
        inputs are still outstanding, or one nobody wants any more."""
        for status in (
            Status.AWAITING_VALIDATION,
            Status.AWAITING,
            Status.PROCESSING,
            Status.CANCELLED,
            Status.ERROR,
        ):
            with self.subTest(status=status):
                scan = ScanFactory(status=status, page_count=2)

                flashed = self._approve(scan)

                self.assertEqual(scan.status, status)
                self.assertIn(PAGE_REVIEW_NOT_READY_MESSAGE, flashed)

    def test_a_get_is_refused(self):
        scan = ScanFactory(
            status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW, page_count=2
        )

        response = self.client.get(
            reverse("approve_page_completeness", kwargs={"pk": scan.pk})
        )

        self.assertEqual(response.status_code, 405)
        scan.refresh_from_db()
        self.assertEqual(
            scan.status, Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )


class TestApprovalSurvivesTheApplyPass(ApplyRunsMixin, ScanningTestCase):
    """An approval outlives a later dots.mocr run.

    The apply pass reaches AWAITING_VALIDATION, the legacy
    PENDING_REVIEW and READY only, so an approved volume is deferred
    whole: no status write, and no rebuild of the Issues a person
    already accepted.
    """

    def test_an_approved_scan_is_left_alone(self):
        scan, _ = self.build(status=Status.PAGE_COMPLETENESS_REVIEW_DONE)

        applied = dots_mocr.apply_ready_runs()

        scan.refresh_from_db()
        self.assertEqual(applied, 0)
        self.assertEqual(scan.status, Status.PAGE_COMPLETENESS_REVIEW_DONE)
        self.assertEqual(scan.ocr_results, [])


class TestRecomputePageNumberIssues(ScanningTestCase):
    """The recompute button of review 1."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)

    def _recompute(self, scan):
        """POST the recompute button and return the flashed messages.

        :param scan: The scan to recompute.
        :returns: The flashed message strings.
        :rtype: list[str]
        """
        response = self.client.post(
            reverse("recalculate", kwargs={"pk": scan.pk})
        )
        self.assertEqual(response.status_code, 302)
        scan.refresh_from_db()
        return [str(m) for m in get_messages(response.wsgi_request)]

    def test_a_dots_scan_is_recomputed_and_keeps_its_review_status(self):
        scan = ScanFactory(
            status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
            start_page=1,
            end_page=2,
            page_count=2,
            ocr_results=dots_results(),
        )

        flashed = self._recompute(scan)

        self.assertIn(RECOMPUTE_DONE_MESSAGE, flashed)
        self.assertTrue(scan.page_map)
        self.assertEqual(
            scan.status, Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )

    def test_an_approved_scan_keeps_its_approval(self):
        scan = ScanFactory(
            status=Status.PAGE_COMPLETENESS_REVIEW_DONE,
            start_page=1,
            end_page=2,
            page_count=2,
            ocr_results=dots_results(),
        )

        self._recompute(scan)

        self.assertEqual(scan.status, Status.PAGE_COMPLETENESS_REVIEW_DONE)

    def test_a_legacy_scan_is_told_instead(self):
        """The retired stage cannot read the volume again, so a rebuild
        of its readings would only look like work (#173)."""
        scan = ScanFactory(
            status=Status.PENDING_REVIEW,
            start_page=1,
            end_page=2,
            page_count=2,
            ocr_results=legacy_results(),
        )

        flashed = self._recompute(scan)

        self.assertIn(LEGACY_OCR_RECOMPUTE_MESSAGE, flashed)
        self.assertNotIn(RECOMPUTE_DONE_MESSAGE, flashed)
        self.assertEqual(scan.page_map, [])

    def test_a_pending_insert_warns_but_does_not_block(self):
        """ "Rebuild & Validate" refuses while the pipeline is paused
        (#173), so blocking here would leave no way forward at all."""
        scan = ScanFactory(
            status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
            start_page=1,
            end_page=2,
            page_count=2,
            ocr_results=dots_results(),
        )
        PageInsert.objects.create(
            scan=scan, logical_page_number=2, image=self.make_image()
        )

        flashed = self._recompute(scan)

        self.assertIn(PENDING_EDITS_SAVED_MESSAGE, flashed)
        self.assertIn(RECOMPUTE_DONE_MESSAGE, flashed)
        self.assertTrue(scan.page_map)
        self.assertTrue(scan.inserts.exists())


class TestRevalidateIsGoneForNewScans(ScanningTestCase):
    """A new-pipeline volume is never re-run from the viewer.

    Sharding, the bitonal conversion and dots.mocr are deterministic,
    so a second run returns the stored answer at the price of another
    doctor conversion and another park out of the review flow. The
    escape hatch is the admin re-queue.
    """

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)

    def _validate(self, scan):
        """POST the re-validate action and return the flashed messages.

        :param scan: The scan to act on.
        :returns: The flashed message strings.
        :rtype: list[str]
        """
        response = self.client.post(
            reverse("start_validate", kwargs={"pk": scan.pk})
        )
        self.assertEqual(response.status_code, 302)
        scan.refresh_from_db()
        return [str(m) for m in get_messages(response.wsgi_request)]

    def test_a_new_scan_is_refused_for_good(self):
        for status in (
            Status.AWAITING_VALIDATION,
            Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
            Status.PAGE_COMPLETENESS_REVIEW_DONE,
        ):
            with self.subTest(status=status):
                scan = ScanFactory(
                    status=status,
                    page_count=2,
                    ocr_results=dots_results(),
                )

                flashed = self._validate(scan)

                self.assertIn(REVALIDATE_UNAVAILABLE_MESSAGE, flashed)
                self.assertEqual(scan.status, status)
                self.assertEqual(scan.queued_action, "")

    def test_a_fresh_upload_is_refused_too(self):
        """It has no readings yet, and no re-run would give it any."""
        scan = ScanFactory(status=Status.AWAITING_VALIDATION)

        flashed = self._validate(scan)

        self.assertIn(REVALIDATE_UNAVAILABLE_MESSAGE, flashed)
        self.assertEqual(scan.status, Status.AWAITING_VALIDATION)

    def test_a_legacy_scan_still_hears_the_paused_message(self):
        """Its stages are gone, not pointless (#173)."""
        from scanning.utils import PIPELINE_PAUSED_MESSAGE

        scan = ScanFactory(
            status=Status.PENDING_REVIEW,
            page_count=2,
            ocr_results=legacy_results(),
        )

        flashed = self._validate(scan)

        self.assertIn(PIPELINE_PAUSED_MESSAGE, flashed)
        self.assertEqual(scan.status, Status.PENDING_REVIEW)


class TestStepOneButtonBar(ScanningTestCase):
    """What review 1 offers, and when.

    The bar is read through ``process_actions``, the fragment the
    viewer refreshes in place. It renders from the same template and
    the same flags as the page, so testing it tests both.
    """

    APPROVE = "I reviewed this scan and it is complete"
    DONE = "Page review done"
    DETECT = "Next: Detect"
    RECOMPUTE = "Recompute page number issues"
    REVALIDATE = "Re-validate"

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)

    def _bar(self, scan):
        """Render the step-1 action bar.

        :param scan: The scan to render it for.
        :returns: The bar's HTML.
        :rtype: str
        """
        response = self.client.get(
            reverse("process_actions", kwargs={"pk": scan.pk}) + "?step=1"
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["html"]

    def test_a_ready_scan_offers_the_approval_and_no_way_past_it(self):
        scan = ScanFactory(
            status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
            page_count=2,
            ocr_results=dots_results(),
        )

        html = self._bar(scan)

        self.assertIn(self.APPROVE, html)
        self.assertIn(self.RECOMPUTE, html)
        self.assertNotIn(self.DETECT, html)
        self.assertNotIn(self.REVALIDATE, html)

    def test_an_approved_scan_says_so_and_opens_detection(self):
        scan = ScanFactory(
            status=Status.PAGE_COMPLETENESS_REVIEW_DONE,
            page_count=2,
            ocr_results=dots_results(),
        )

        html = self._bar(scan)

        self.assertIn(self.DONE, html)
        self.assertNotIn(self.APPROVE, html)
        self.assertIn(self.DETECT, html)
        self.assertNotIn(self.REVALIDATE, html)

    def test_open_issues_do_not_hide_the_approval(self):
        """The old bar hid the way forward until the issue list was
        empty. Review 1 asks the person instead."""
        scan = ScanFactory(
            status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
            page_count=2,
            missing_pages=[2],
            ocr_results=dots_results(),
        )

        html = self._bar(scan)

        self.assertIn(self.APPROVE, html)

    def test_a_fresh_upload_offers_no_validate_button(self):
        """The other branch of the bar: no page count yet, and still no
        re-run to offer."""
        scan = ScanFactory(status=Status.AWAITING_VALIDATION)

        html = self._bar(scan)

        self.assertNotIn("Validate", html)

    def test_a_legacy_scan_keeps_the_old_bar(self):
        """These rows never reach the #154 states, so gating step 2 on
        an approval they cannot give would strand them."""
        scan = ScanFactory(
            status=Status.PENDING_REVIEW,
            page_count=2,
            ocr_results=legacy_results(),
        )
        Detection.objects.create(
            scan=scan,
            page_index=0,
            label="KEY",
            label_id=0,
            confidence=0.9,
            x0=0,
            y0=0,
            x1=10,
            y1=10,
            img_width=100,
            img_height=100,
        )

        html = self._bar(scan)

        self.assertIn(self.DETECT, html)
        self.assertNotIn(self.APPROVE, html)
        self.assertIn(self.REVALIDATE, html)


class TestStepOneGoal(ScanningTestCase):
    """The page states what review 1 is for."""

    def test_step_1_names_its_objective(self):
        user = self.make_user()
        self.client.force_login(user)
        scan = ScanFactory(
            status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
            page_count=2,
            ocr_results=dots_results(),
        )

        response = self.client.get(
            reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=1"
        )

        self.assertContains(response, "Goal: make sure this volume is page")
        self.assertContains(response, "missing, duplicated, mislabeled")
