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

import json
import pathlib

from django.contrib.messages import get_messages
from django.urls import reverse

from scanning import dots_mocr, page_edits, page_numbers, services
from scanning.factories import (
    ExternalJobFactory,
    PageEditFactory,
    ScanFactory,
)
from scanning.models import (
    BUSY_STATUSES,
    REVIEW_STATUSES,
    CheckName,
    Detection,
    Issue,
    JobEngine,
    JobStage,
    JobStatus,
    PageEdit,
    PageRepairRequest,
    Scan,
    Status,
)
from scanning.tests.test_dots_mocr_apply import ApplyRunsMixin
from scanning.tests.test_views import ScanningTestCase
from scanning.views_process import (
    LEGACY_OCR_RECOMPUTE_MESSAGE,
    PAGE_REVIEW_ALREADY_DONE_MESSAGE,
    PAGE_REVIEW_APPROVAL_REQUIRED_MESSAGE,
    PAGE_REVIEW_APPROVED_MESSAGE,
    PAGE_REVIEW_NOT_READY_MESSAGE,
    PENDING_EDITS_SAVED_MESSAGE,
    RECOMPUTE_DONE_MESSAGE,
    REPAIRS_WAITING_MESSAGE,
    REVALIDATE_UNAVAILABLE_MESSAGE,
    WATCHED_STATUSES,
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

    def test_another_engine_s_zone_is_the_new_pipeline(self):
        """A number Mistral or Surya filled in (#351) is a model read."""
        results = legacy_results()
        results[0]["zone"] = "mistral-header"
        scan = ScanFactory(ocr_results=results)
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

    def test_a_dead_run_does_not_flip_a_legacy_scan(self):
        """A failed, cancelled or expired run delivered nothing, so
        the scan's readings are still the retired stage's, and "run
        OCR again" stays the right advice."""
        for status in (
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.EXPIRED,
        ):
            with self.subTest(status=status):
                scan = ScanFactory(ocr_results=legacy_results())
                ExternalJobFactory(
                    scan=scan,
                    stage=JobStage.ANALYZE,
                    engine=JobEngine.DOTS_MOCR,
                    status=status,
                )
                self.assertTrue(services.has_legacy_ocr(scan))


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


class TestDetectRequiresTheApproval(ScanningTestCase):
    """The view enforces the gate the bar draws (#151).

    A scan still in READY has a pending approval by definition, so a
    direct POST to ``start_detect`` is sent back to step 1. A legacy
    scan never holds READY, so the old rows keep their shortcut --
    ``TestStartDetectSkipsIfExists`` covers that side.
    """

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)

    def _detect(self, scan):
        """POST the detect action and return the response.

        :param scan: The scan to act on.
        :returns: The redirect response.
        """
        response = self.client.post(
            reverse("start_detect", kwargs={"pk": scan.pk})
        )
        self.assertEqual(response.status_code, 302)
        return response

    def _make_detection(self, scan):
        """Give the scan one detection, so the step-2 shortcut opens.

        :param scan: The scan to attach it to.
        """
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

    def test_a_ready_scan_is_sent_back_to_its_review(self):
        scan = ScanFactory(
            status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
            page_count=2,
            ocr_results=dots_results(),
        )
        self._make_detection(scan)

        response = self._detect(scan)

        self.assertEqual(
            response["Location"],
            reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=1",
        )
        flashed = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertIn(PAGE_REVIEW_APPROVAL_REQUIRED_MESSAGE, flashed)

    def test_an_approved_scan_passes_to_step_2(self):
        scan = ScanFactory(
            status=Status.PAGE_COMPLETENESS_REVIEW_DONE,
            page_count=2,
            ocr_results=dots_results(),
        )
        self._make_detection(scan)

        response = self._detect(scan)

        self.assertEqual(
            response["Location"],
            reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=2",
        )

    def test_an_approved_scan_with_an_open_edit_hears_about_the_edit(self):
        """The pending guard runs after the approval check (#232).

        The other order told a reviewer who had approved to approve
        again, and sent them to a step 1 with no approve button.
        """
        scan = ScanFactory(
            status=Status.PAGE_COMPLETENESS_REVIEW_DONE,
            page_count=2,
            ocr_results=dots_results(),
        )
        self._make_detection(scan)
        PageEditFactory(
            scan=scan, kind=PageEdit.Kind.DELETE_PAGE, pdf_page=1, value=""
        )

        response = self._detect(scan)

        self.assertEqual(
            response["Location"],
            reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=1",
        )
        flashed = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertNotIn(PAGE_REVIEW_APPROVAL_REQUIRED_MESSAGE, flashed)
        self.assertTrue(
            any("not built into the volume yet" in m for m in flashed),
            flashed,
        )


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

    def test_a_stale_recompute_cannot_undo_a_concurrent_approval(self):
        """The approve button flips READY -> DONE while a recompute
        holds a scan it fetched as READY. The recompute decides the
        status on the row as it is in the DB, never on its own stale
        copy, so the approval must survive the save."""
        scan = ScanFactory(
            status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
            start_page=1,
            end_page=2,
            page_count=2,
            ocr_results=dots_results(),
        )
        stale = Scan.objects.get(pk=scan.pk)
        Scan.objects.filter(pk=scan.pk).update(
            status=Status.PAGE_COMPLETENESS_REVIEW_DONE
        )

        services.recalculate_issues(stale)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.PAGE_COMPLETENESS_REVIEW_DONE)
        self.assertEqual(stale.status, Status.PAGE_COMPLETENESS_REVIEW_DONE)
        self.assertTrue(scan.page_map)

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
        PageEdit.objects.create(
            scan=scan,
            kind=PageEdit.Kind.INSERT_PAGE,
            anchor_pdf_page=1,
            logical_page="2",
            image=self.make_image(),
        )

        flashed = self._recompute(scan)

        self.assertIn(PENDING_EDITS_SAVED_MESSAGE, flashed)
        self.assertIn(RECOMPUTE_DONE_MESSAGE, flashed)
        self.assertTrue(scan.page_map)
        self.assertTrue(
            scan.page_edits.filter(kind=PageEdit.Kind.INSERT_PAGE).exists()
        )


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

    def test_an_approved_scan_without_detections_is_promised_no_run(self):
        """start_detect starts nothing (#195/#196), so the button must not
        confirm a paid RunPod run; it says where the run stands (#250)."""
        scan = ScanFactory(
            status=Status.PAGE_COMPLETENESS_REVIEW_DONE,
            page_count=2,
            ocr_results=dots_results(),
        )

        html = self._bar(scan)

        self.assertIn(self.DETECT, html)
        self.assertNotIn("incur costs", html.split("start_detect")[-1])
        self.assertIn("Detection starts by itself after the upload", html)
        # A multi-line ``{# #}`` is not a comment to Django; it used to
        # render this text into the bar.
        self.assertNotIn("No paid confirm here", html)

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

    def test_a_parked_scan_is_offered_no_way_forward(self):
        """A new-pipeline volume whose review has not begun holds no
        issue rows, which used to read as "nothing to fix" and reveal
        "Next: Detect" -- a paid RunPod confirm that start_detect then
        refuses. Approval is the gate; a scan that cannot be approved
        yet is offered nothing."""
        scan = ScanFactory(
            status=Status.AWAITING_VALIDATION,
            page_count=2,
            ocr_results=dots_results(),
        )

        html = self._bar(scan)

        self.assertNotIn(self.DETECT, html)
        self.assertNotIn(self.APPROVE, html)
        self.assertNotIn(self.REVALIDATE, html)

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


class TestTheCardOfAMissingPageAfterFrontMatter(ScanningTestCase):
    """A card of a printed number goes to that number, not the front matter.

    An unnumbered page carries its PDF page as its display number, so
    with 13 pages of front matter, the card of printed page 10 went to
    PDF page 10, and a card of printed page 12 to PDF page 12 as well as
    to the page that prints 12. Scan 3156 has this shape (#403).
    """

    def _scan(self, numbers=None):
        """Create a volume with 13 unnumbered pages, then ``numbers``.

        :param numbers: The printed numbers after the front matter. By
            default 1-9 and 12-20, so printed pages 10 and 11 are
            missing after PDF page 22.
        :returns: The scan, with its issues computed.
        """
        if numbers is None:
            numbers = list(range(1, 10)) + list(range(12, 21))
        results = [
            {"pdf_page": p, "detected": "", "type": "single"}
            for p in range(1, 14)
        ]
        results += [
            {"pdf_page": 14 + i, "detected": str(n), "type": "single"}
            for i, n in enumerate(numbers)
        ]
        scan = ScanFactory(
            status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
            page_count=len(results),
            start_page=1,
            end_page=20,
            ocr_results=results,
        )
        pathlib.Path(scan.original_pdf.path).unlink()
        services.recalculate_issues(scan)
        return scan

    def _step_one(self, scan):
        """Render step 1 of the scan.

        :param scan: The scan.
        :returns: The response.
        """
        self.client.force_login(self.make_user())
        return self.client.get(
            reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=1"
        )

    def _cards(self, response, check):
        """Return each card of one check with the index it navigates to.

        :param response: The step-1 response.
        :param check: A ``CheckName`` value.
        :returns: ``{page_number: nav_pdf_index}``.
        :rtype: dict
        """
        return {
            i.page_number: i.nav_pdf_index
            for i in response.context["issues"]
            if i.check_name == check
        }

    def test_the_card_navigates_to_the_page_before_the_gap(self):
        response = self._step_one(self._scan())

        self.assertEqual(
            self._cards(response, CheckName.MISSING_PAGE), {10: 21, 11: 21}
        )

    def test_the_card_stays_at_the_gap_after_an_upload_into_it(self):
        """An upload takes the placeholder's entry, and the card stands
        until a recheck: it must not fall back to the front matter."""
        scan = self._scan()
        PageEditFactory(
            scan=scan,
            kind=PageEdit.Kind.INSERT_PAGE,
            pdf_page=None,
            anchor_pdf_page=22,
            logical_page="10",
            value="",
            source_fingerprint=scan.source_fingerprint,
        )

        response = self._step_one(scan)

        self.assertEqual(
            self._cards(response, CheckName.MISSING_PAGE), {10: 21, 11: 21}
        )

    def test_a_duplicate_card_names_only_the_pages_that_print_it(self):
        """Printed page 12 on PDF pages 23 and 24: the card goes to 23,
        and PDF page 12 of the front matter gets no red border from it."""
        numbers = list(range(1, 10)) + [12, 12] + list(range(13, 21))
        scan = self._scan(numbers)
        scan.issues.exclude(check_name=CheckName.DUPLICATE_PAGE).delete()

        response = self._step_one(scan)

        self.assertEqual(
            self._cards(response, CheckName.DUPLICATE_PAGE), {12: 22}
        )
        flagged = json.loads(response.context["flagged_indices_json"])
        self.assertEqual(sorted(flagged), [22, 23])


class TestTheCardOfARangeMissingAtTheEnd(ScanningTestCase):
    """The card of a trailing gap must reach its placeholder (#256).

    The card says "ask a scanner for them at the placeholder at the end
    of the volume", so the click has to land there. Its own address is a
    printed number the volume does not show, and the placeholder carries
    the range as its label, so neither of ``goToPage``'s label lookups
    finds it. The physical address does.
    """

    def _step_one(self):
        """Render step 1 of a volume that stops 10 pages early.

        :returns: The response.
        """
        user = self.make_user()
        self.client.force_login(user)
        scan = ScanFactory(
            status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
            page_count=10,
            start_page=1,
            end_page=20,
            ocr_results=dots_results(10),
        )
        # As in production, where the recompute runs on a web pod that
        # never pulled the original: the page count stands as stored.
        pathlib.Path(scan.original_pdf.path).unlink()
        services.recalculate_issues(scan)
        return self.client.get(
            reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=1"
        )

    def test_the_card_navigates_to_the_page_the_volume_stops_at(self):
        response = self._step_one()

        card = next(
            i
            for i in response.context["issues"]
            if i.check_name == CheckName.LARGE_GAP
        )
        self.assertEqual(card.page_number, 11)
        self.assertEqual(card.nav_pdf_index, 9)

    def test_the_last_page_of_the_volume_keeps_no_red_border(self):
        """It is not itself at fault; the card only navigates to it."""
        response = self._step_one()

        flagged = json.loads(response.context["flagged_indices_json"])
        self.assertNotIn(9, flagged)

    def test_the_placeholder_reaches_the_viewer(self):
        response = self._step_one()

        self.assertContains(response, "missing_range")
        self.assertContains(response, "11-20")


def unread_results(count=3, without=(2,)):
    """Build ``ocr_results`` where some pages carry no reading.

    :param count: How many pages to describe.
    :param without: The pages the reader left with no number.
    :returns: One entry per page, in page order.
    :rtype: list[dict]
    """
    return [
        {
            "pdf_page": page,
            "detected": None if page in without else str(page),
            "type": None if page in without else "single",
            "zone": "dots-header",
        }
        for page in range(1, count + 1)
    ]


class TestPagesWithoutNumber(ScanningTestCase):
    """The rule of the second review-1 gate (#342).

    It answers one question -- which pages of this volume carry no page
    number -- from the data, never from the ``Issue`` rows.
    """

    def test_a_page_with_no_reading_is_named(self):
        scan = ScanFactory(ocr_results=unread_results())

        self.assertEqual(page_numbers.pages_without_number(scan), [2])

    def test_a_volume_every_page_of_which_is_read_names_nobody(self):
        scan = ScanFactory(ocr_results=dots_results(3))

        self.assertEqual(page_numbers.pages_without_number(scan), [])

    def test_a_scan_with_no_readings_names_nobody(self):
        """The glue has not run, so the question has no answer yet.
        Such a volume is not in READY either."""
        self.assertEqual(page_numbers.pages_without_number(ScanFactory()), [])

    def test_a_trailing_letter_is_a_reading(self):
        """A page like 2094a claims no number in the sequence (#319),
        but a reader read it, so it is not a hole."""
        results = unread_results(without=())
        results[1]["detected"] = "2094a"
        results[1]["type"] = "suffixed"
        scan = ScanFactory(ocr_results=results)

        self.assertEqual(page_numbers.pages_without_number(scan), [])

    def test_a_range_is_a_reading(self):
        results = unread_results(without=())
        results[1]["detected"] = "677-685"
        results[1]["type"] = "range"
        scan = ScanFactory(ocr_results=results)

        self.assertEqual(page_numbers.pages_without_number(scan), [])

    def test_a_number_the_curator_typed_answers_its_page(self):
        """With no recompute: the overlay comes before the count, so
        the approve button comes back on the next render."""
        scan = ScanFactory(ocr_results=unread_results())
        PageEditFactory(scan=scan, pdf_page=2, value="17")

        self.assertEqual(page_numbers.pages_without_number(scan), [])

    def test_a_number_the_curator_cleared_answers_its_page(self):
        """Emptying the field is the gesture the page editor offers
        for a page with no number, and the row says a person did it.
        ``assign_page`` deletes the card of the page it writes,
        whatever it writes, so a rule that ignored the row would name
        a page whose card nobody can reach until the next recompute."""
        scan = ScanFactory(ocr_results=dots_results(3))
        PageEditFactory(scan=scan, pdf_page=2, value="")

        self.assertEqual(page_numbers.pages_without_number(scan), [])

    def test_a_cleared_number_of_another_original_answers_nothing(self):
        """The #214 rule, on the page the reader left with none: the
        overlay skips the stale row and so does the count."""
        scan = ScanFactory(
            ocr_results=unread_results(), source_fingerprint="abc"
        )
        PageEditFactory(
            scan=scan, pdf_page=2, value="", source_fingerprint="def"
        )

        self.assertEqual(page_numbers.pages_without_number(scan), [2])

    def test_a_page_marked_for_deletion_is_not_named(self):
        scan = ScanFactory(ocr_results=unread_results())
        PageEditFactory(
            scan=scan,
            kind=PageEdit.Kind.DELETE_PAGE,
            pdf_page=2,
            value="",
        )

        self.assertEqual(page_numbers.pages_without_number(scan), [])

    def test_a_dismissed_card_answers_its_page(self):
        """A cover, a blank leaf and a plate carry no printed number,
        and the dismissal is how a person says so."""
        scan = ScanFactory(ocr_results=unread_results())
        PageEditFactory(
            scan=scan,
            kind=PageEdit.Kind.DISMISS_ISSUE,
            pdf_page=2,
            value=CheckName.NO_PAGE_NUMBER,
        )

        self.assertEqual(page_numbers.pages_without_number(scan), [])

    def test_a_dismissal_of_another_check_answers_nothing(self):
        scan = ScanFactory(ocr_results=unread_results())
        PageEditFactory(
            scan=scan,
            kind=PageEdit.Kind.DISMISS_ISSUE,
            pdf_page=2,
            value=CheckName.BLANK_PAGE,
        )

        self.assertEqual(page_numbers.pages_without_number(scan), [2])

    def test_a_withdrawn_dismissal_names_the_page_again(self):
        scan = ScanFactory(ocr_results=unread_results())
        row = PageEditFactory(
            scan=scan,
            kind=PageEdit.Kind.DISMISS_ISSUE,
            pdf_page=2,
            value=CheckName.NO_PAGE_NUMBER,
        )
        page_edits.withdraw(
            PageEdit.objects.filter(pk=row.pk), self.make_user(username="w")
        )

        self.assertEqual(page_numbers.pages_without_number(scan), [2])

    def test_a_dismissal_against_another_original_hides_no_page(self):
        """The #214 rule: an acting reader takes the current rows."""
        scan = ScanFactory(
            ocr_results=unread_results(), source_fingerprint="abc"
        )
        PageEditFactory(
            scan=scan,
            kind=PageEdit.Kind.DISMISS_ISSUE,
            pdf_page=2,
            value=CheckName.NO_PAGE_NUMBER,
            source_fingerprint="def",
        )

        self.assertEqual(page_numbers.pages_without_number(scan), [2])

    def test_the_pages_come_back_in_page_order(self):
        scan = ScanFactory(ocr_results=unread_results(5, without=(4, 1, 2)))

        self.assertEqual(page_numbers.pages_without_number(scan), [1, 2, 4])

    def test_the_issue_rows_are_not_the_source(self):
        """A card nobody rebuilt says nothing about the pages."""
        scan = ScanFactory(ocr_results=dots_results(3))
        Issue.objects.create(
            scan=scan,
            check_name=CheckName.NO_PAGE_NUMBER,
            severity=Issue.Severity.INFO,
            page_number=2,
            message="No page number detected on PDF page 2.",
        )

        self.assertEqual(page_numbers.pages_without_number(scan), [])


class TestThePageNumberGate(ScanningTestCase):
    """The approval, the bar and the fragment under the gate (#342)."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.scan = ScanFactory(
            status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
            page_count=3,
            ocr_results=unread_results(),
        )

    def _approve(self):
        """POST the approve button and return the flashed messages.

        :returns: The flashed message strings.
        :rtype: list[str]
        """
        response = self.client.post(
            reverse("approve_page_completeness", kwargs={"pk": self.scan.pk})
        )
        self.scan.refresh_from_db()
        return [str(m) for m in get_messages(response.wsgi_request)]

    def _ask_for_a_page(self):
        """Ask a scanner for one page, so the other gate refuses too.

        :returns: None.
        """
        PageRepairRequest.objects.create(
            scan=self.scan,
            action=PageRepairRequest.Action.REPLACE,
            requested_by=self.user,
            pdf_page=3,
        )

    def _step_one(self):
        """Render step 1 of the processing page.

        :returns: The response.
        """
        return self.client.get(
            reverse("scan_process", kwargs={"pk": self.scan.pk}) + "?step=1"
        )

    def test_the_approval_is_refused(self):
        flashed = self._approve()

        self.assertEqual(
            self.scan.status, Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )
        self.assertEqual(len(flashed), 1)
        self.assertIn("1 page with no page number: 2", flashed[0])

    def test_the_message_names_the_pages(self):
        self.scan.ocr_results = unread_results(5, without=(2, 4))
        self.scan.save(update_fields=["ocr_results"])

        flashed = self._approve()

        self.assertIn("2 pages with no page number: 2, 4", flashed[0])

    def test_the_message_counts_the_pages_it_does_not_name(self):
        self.scan.ocr_results = unread_results(12, without=tuple(range(1, 12)))
        self.scan.save(update_fields=["ocr_results"])

        flashed = self._approve()

        self.assertIn("1, 2, 3, 4, 5, 6, 7, 8 and 3 more", flashed[0])

    def test_the_approval_passes_once_the_number_is_typed(self):
        PageEditFactory(scan=self.scan, pdf_page=2, value="17")

        flashed = self._approve()

        self.assertEqual(
            self.scan.status, Status.PAGE_COMPLETENESS_REVIEW_DONE
        )
        self.assertIn(PAGE_REVIEW_APPROVED_MESSAGE, flashed)

    def test_the_approval_passes_once_the_card_is_dismissed(self):
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.DISMISS_ISSUE,
            pdf_page=2,
            value=CheckName.NO_PAGE_NUMBER,
        )

        flashed = self._approve()

        self.assertEqual(
            self.scan.status, Status.PAGE_COMPLETENESS_REVIEW_DONE
        )
        self.assertIn(PAGE_REVIEW_APPROVED_MESSAGE, flashed)

    def test_the_two_refusals_do_not_hide_each_other(self):
        """A reviewer who answers one must see the other at once."""
        self._ask_for_a_page()

        flashed = self._approve()

        self.assertEqual(len(flashed), 2)
        self.assertEqual(flashed[0], REPAIRS_WAITING_MESSAGE)
        self.assertIn("no page number", flashed[1])
        self.assertEqual(
            self.scan.status, Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )

    def test_a_volume_past_review_one_hears_the_status(self):
        """Its pages are locked, so "type the number" would name work
        nobody can do. The compare-and-swap owns the answer there."""
        self.scan.status = Status.PAGE_COMPLETENESS_REVIEW_DONE
        self.scan.save(update_fields=["status"])

        flashed = self._approve()

        self.assertEqual(flashed, [PAGE_REVIEW_ALREADY_DONE_MESSAGE])

    def test_a_volume_that_is_not_ready_hears_the_status(self):
        self.scan.status = Status.ERROR
        self.scan.save(update_fields=["status"])

        flashed = self._approve()

        self.assertEqual(flashed, [PAGE_REVIEW_NOT_READY_MESSAGE])

    def test_the_bar_shows_the_note_and_no_button(self):
        response = self._step_one()

        self.assertEqual(response.context["pages_without_number"], [2])
        self.assertContains(response, "1 page with no number")
        self.assertNotContains(
            response, "I reviewed this scan and it is complete"
        )

    def test_the_bar_shows_both_notes(self):
        self._ask_for_a_page()

        response = self._step_one()

        self.assertContains(response, "Waiting for a scanner")
        self.assertContains(response, "1 page with no number")

    def test_the_note_carries_the_first_page(self):
        """``goToPage`` reads ``data-pdf-index``, which is 0-based."""
        self.scan.ocr_results = unread_results(5, without=(3, 5))
        self.scan.save(update_fields=["ocr_results"])

        self.assertContains(self._step_one(), 'data-pdf-index="2"')

    def test_the_fragment_agrees_with_the_page(self):
        """One flag serves both, or the bar would offer a refused
        button on one of them."""
        fragment = self.client.get(
            reverse("process_actions", kwargs={"pk": self.scan.pk}) + "?step=1"
        )

        self.assertIn(
            "1 page with no number", json.loads(fragment.content)["html"]
        )

    def test_the_bar_gives_the_button_back(self):
        PageEditFactory(scan=self.scan, pdf_page=2, value="17")

        response = self._step_one()

        self.assertEqual(response.context["pages_without_number"], [])
        self.assertContains(
            response, "I reviewed this scan and it is complete"
        )

    def test_a_volume_past_review_one_is_not_measured(self):
        """The rule is for new approvals, and every other status pays
        no query for it."""
        self.scan.status = Status.PAGE_COMPLETENESS_REVIEW_DONE
        self.scan.save(update_fields=["status"])

        response = self._step_one()

        self.assertEqual(response.context["pages_without_number"], [])
        self.assertContains(response, "Page review done")


class TestTheParkedPageWatches(ScanningTestCase):
    """A page parked in AWAITING_VALIDATION polls until READY (#332).

    The bitonal merge parks the scan there while dots.mocr still reads,
    and the busy poller's reload is the last one. The page watches that
    status at its own cadence, and no other status that is not busy.
    """

    POLLER = "scanning/viewer_progress.js"

    def setUp(self):
        self.client.force_login(self.make_user())

    def _step_one(self, status):
        """Render step 1 of a scan in one status.

        :param status: The status the scan holds.
        :returns: The response.
        """
        scan = ScanFactory(
            status=status, page_count=2, ocr_results=dots_results()
        )
        return self.client.get(
            reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=1"
        )

    def test_the_parked_page_watches_its_status(self):
        response = self._step_one(Status.AWAITING_VALIDATION)

        self.assertTrue(response.context["watches_status"])
        self.assertContains(response, self.POLLER)
        self.assertContains(response, "progressWatch: 'awaiting_validation'")
        self.assertContains(response, 'id="awaiting-msg"')

    def test_a_busy_page_polls_without_watching(self):
        response = self._step_one(Status.AWAITING)

        self.assertContains(response, self.POLLER)
        self.assertNotContains(response, "progressWatch")

    def test_a_review_page_polls_nothing(self):
        response = self._step_one(Status.READY_FOR_PAGE_COMPLETENESS_REVIEW)

        self.assertFalse(response.context["watches_status"])
        self.assertNotContains(response, self.POLLER)
        self.assertNotContains(response, "progressUrl")

    def test_the_watch_poll_leaves_out_the_pages_and_the_log(self):
        """The watch polls every five seconds for as long as the park
        lasts, and reads the status and the run summaries alone."""
        scan = ScanFactory(
            status=Status.AWAITING_VALIDATION,
            page_count=2,
            ocr_results=dots_results(),
        )
        url = reverse("progress_api", kwargs={"pk": scan.pk})

        watched = self.client.get(url + "?watch=1").json()
        busy = self.client.get(url).json()

        self.assertEqual(watched["status"], Status.AWAITING_VALIDATION)
        self.assertNotIn("ocr_results", watched)
        self.assertNotIn("log", watched)
        self.assertIn("ocr_results", busy)
        self.assertIn("log", busy)

    def test_a_watched_status_is_neither_busy_nor_a_review(self):
        """A watched status is the viewer's question alone: it must not
        join the stale sweep or the unpolled review states."""
        self.assertFalse(WATCHED_STATUSES & BUSY_STATUSES)
        self.assertFalse(WATCHED_STATUSES & REVIEW_STATUSES)
