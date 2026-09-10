"""Tests for the findings of review 2 (issue #240, PR D).

The module ``findings`` derives every finding from the detection,
boundary and redaction rows and writes them as ``Issue`` rows; the
curator's dismissals are ``ReviewDismissal`` rows the rebuild resolves
onto the new findings. Tested here: the rebuild, the resolution, the
decisions, the endpoints, the two lists of the view, and the confirm of
the approve button.
"""

import json
from io import StringIO

from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from scanning import findings
from scanning.factories import OpinionBoundaryFactory, ScanFactory
from scanning.models import (
    REVIEW2_CHECKS,
    ApplyRun,
    CheckName,
    Detection,
    DetectionDecision,
    Issue,
    OpinionBoundary,
    PageEdit,
    Redaction,
    ReviewDismissal,
    Status,
)
from scanning.tests.test_views import ScanningTestCase

FINGERPRINT = "10:4"


def make_detection(scan, label, page_index=0, **fields):
    """Create one live model detection.

    :param scan: The scan.
    :param label: The class label.
    :param page_index: The 0-based page.
    :param fields: Overrides.
    :returns: The row.
    """
    values = {
        "scan": scan,
        "page_index": page_index,
        "label": label,
        "label_id": {"KEY_ICON": 1, "CASE_CAPTION": 3, "HEADNOTE": 5}.get(
            label, 9
        ),
        "confidence": 0.9,
        "x0": 100.0,
        "y0": 100.0,
        "x1": 200.0,
        "y1": 200.0,
        "img_width": 1700,
        "img_height": 2200,
        "model_name": Detection.ModelName.BL_WARM,
        "model_count": 1,
        "found_by": [{"model": "bl_warm", "confidence": 0.9}],
        "source_page": page_index + 1,
        "source_fingerprint": scan.source_fingerprint,
    }
    values.update(fields)
    return Detection.objects.create(**values)


def make_boundary(scan, caption, key, **fields):
    """Create one computed boundary from a caption and a key row.

    :param scan: The scan.
    :param caption: The caption ``Detection``.
    :param key: The key icon ``Detection``.
    :param fields: Overrides.
    :returns: The row.
    """
    values = {
        "scan": scan,
        "start_source_page": caption.page_index + 1,
        "start_page_index": caption.page_index,
        "start_y": caption.y0 * 72 / 200,
        "end_source_page": key.page_index + 1,
        "end_page_index": key.page_index,
        "end_y": key.y1 * 72 / 200,
        "start_detection": caption,
        "end_detection": key,
        "source_fingerprint": scan.source_fingerprint,
    }
    values.update(fields)
    return OpinionBoundaryFactory(**values)


def make_redaction(scan, page_index=0, **fields):
    """Create one computed black redaction, in points.

    :param scan: The scan.
    :param page_index: The 0-based page.
    :param fields: Overrides.
    :returns: The row.
    """
    values = {
        "scan": scan,
        "origin": Redaction.Origin.COMPUTED,
        "rect_type": "headnote",
        "fill": Redaction.Fill.BLACK,
        "x0": 30.0,
        "y0": 30.0,
        "x1": 80.0,
        "y1": 80.0,
        "source_page": page_index + 1,
        "source_fingerprint": scan.source_fingerprint,
        "page_index": page_index,
    }
    values.update(fields)
    return Redaction.objects.create(**values)


def make_scan(**fields):
    """Create a scan of four pages in review 2.

    :param fields: Overrides.
    :returns: The scan.
    """
    values = {
        "page_count": 4,
        "start_page": 1,
        "end_page": 4,
        "status": Status.READY_FOR_REDACTION_REVIEW,
        "source_fingerprint": FINGERPRINT,
    }
    values.update(fields)
    return ScanFactory(**values)


def checks_of(scan) -> list[str]:
    """Return the review-2 check names written for ``scan``, sorted."""
    return sorted(
        scan.issues.filter(check_name__in=REVIEW2_CHECKS).values_list(
            "check_name", flat=True
        )
    )


class TestRebuild(ScanningTestCase):
    """Every finding is derived from the rows."""

    def test_no_measured_finding_without_a_computed_boundary(self):
        """A volume the compute has not reached has no pairing, so every
        key icon would read as unmatched; that is a fact about the
        queue, not a finding."""
        scan = make_scan()
        make_detection(scan, "KEY_ICON")
        make_detection(scan, "HEADNOTE", confidence=0.95)

        self.assertEqual(findings.rebuild(scan), 0)

        self.assertEqual(checks_of(scan), [])

    def test_a_key_icon_no_boundary_names_is_unmatched(self):
        scan = make_scan()
        caption = make_detection(scan, "CASE_CAPTION", 0)
        key = make_detection(scan, "KEY_ICON", 1)
        loose = make_detection(scan, "KEY_ICON", 2, x0=300.0, x1=400.0)
        make_boundary(scan, caption, key)

        findings.rebuild(scan)

        rows = scan.issues.filter(check_name=CheckName.UNMATCHED_KEY_ICON)
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.target, Issue.Target.DETECTION)
        self.assertEqual(row.page_number, 3)
        self.assertEqual(row.metadata["detection_id"], loose.pk)
        self.assertEqual(row.metadata["bbox"], [300.0, 100.0, 400.0, 200.0])
        self.assertEqual(row.metadata["source_page"], 3)
        self.assertEqual(row.severity, Issue.Severity.WARNING)

    def test_a_dismissed_boundary_frees_its_key(self):
        scan = make_scan()
        caption = make_detection(scan, "CASE_CAPTION", 0)
        key = make_detection(scan, "KEY_ICON", 1)
        boundary = make_boundary(scan, caption, key)
        dismissal = OpinionBoundaryFactory(
            scan=scan,
            origin=OpinionBoundary.Origin.HUMAN,
            kind=OpinionBoundary.Kind.DISMISS,
            start_detection=None,
            end_detection=None,
        )
        OpinionBoundary.objects.filter(pk=boundary.pk).update(
            decision=dismissal
        )

        findings.rebuild(scan)

        self.assertIn(CheckName.UNMATCHED_KEY_ICON, checks_of(scan))
        self.assertIn(CheckName.UNMATCHED_CAPTION, checks_of(scan))

    def test_a_caption_between_two_paired_keys_is_a_continuation(self):
        """A caption inside an opinion's span is not a missed opinion;
        one past the last key is."""
        scan = make_scan()
        c1 = make_detection(scan, "CASE_CAPTION", 0)
        k1 = make_detection(scan, "KEY_ICON", 1, y0=500.0, y1=600.0)
        c2 = make_detection(scan, "CASE_CAPTION", 2)
        k2 = make_detection(scan, "KEY_ICON", 3, y0=500.0, y1=600.0)
        make_boundary(scan, c1, k1)
        make_boundary(scan, c2, k2)
        inside = make_detection(scan, "CASE_CAPTION", 1, y0=900.0, y1=950.0)
        after = make_detection(scan, "CASE_CAPTION", 3, y0=900.0, y1=950.0)

        findings.rebuild(scan)

        rows = scan.issues.filter(check_name=CheckName.UNMATCHED_CAPTION)
        self.assertEqual(
            sorted(r.metadata["detection_id"] for r in rows), [after.pk]
        )
        self.assertNotIn(inside.pk, [r.metadata["detection_id"] for r in rows])

    def test_pages_no_opinion_covers_are_one_finding_per_run(self):
        scan = make_scan()
        caption = make_detection(scan, "CASE_CAPTION", 0)
        key = make_detection(scan, "KEY_ICON", 1)
        make_boundary(scan, caption, key)

        findings.rebuild(scan)

        row = scan.issues.get(check_name=CheckName.UNCOVERED_PAGES)
        self.assertEqual(row.target, Issue.Target.PAGES)
        self.assertEqual(
            row.message, "Pages 3-4 (2 pages) not covered by any opinion"
        )
        self.assertEqual(row.page_number, 3)
        self.assertEqual(row.metadata["first_index"], 2)
        self.assertEqual(row.metadata["last_index"], 3)
        self.assertEqual(row.metadata["source_page"], 3)
        self.assertEqual(row.metadata["end_source_page"], 4)

    def test_a_headnote_no_black_box_covers_is_a_finding(self):
        """The box centre (150, 150 px at 200 dpi = 54 pt) is tested
        against the visible black redactions of its page, in points."""
        scan = make_scan()
        caption = make_detection(scan, "CASE_CAPTION", 0)
        key = make_detection(scan, "KEY_ICON", 3)
        make_boundary(scan, caption, key)
        covered = make_detection(scan, "HEADNOTE", 0, confidence=0.95)
        make_redaction(scan, 0)  # 30..80 pt holds 54, 54
        bare = make_detection(scan, "HEADNOTE", 1, confidence=0.95)
        make_redaction(scan, 1, fill=Redaction.Fill.WHITE, rect_type="margin")
        make_detection(scan, "HEADNOTE", 2, confidence=0.5)  # not confident
        hidden = make_detection(scan, "HEADNOTE", 3, confidence=0.95)
        make_redaction(scan, 3, decision=None)
        dismissed = Redaction.objects.create(
            scan=scan,
            origin=Redaction.Origin.HUMAN,
            kind=Redaction.Kind.DISMISS,
            rect_type="headnote",
            fill=Redaction.Fill.BLACK,
            source_page=4,
            page_index=3,
            source_fingerprint=FINGERPRINT,
        )
        Redaction.objects.filter(scan=scan, page_index=3).exclude(
            pk=dismissed.pk
        ).update(decision=dismissed)

        findings.rebuild(scan)

        rows = scan.issues.filter(check_name=CheckName.UNCOVERED_HEADNOTE)
        self.assertEqual(
            sorted(r.metadata["detection_id"] for r in rows),
            sorted([bare.pk, hidden.pk]),
        )
        self.assertNotIn(
            covered.pk, [r.metadata["detection_id"] for r in rows]
        )
        self.assertEqual(rows.first().target, Issue.Target.REDACTION)

    def test_a_curator_drawn_black_box_covers_a_headnote(self):
        scan = make_scan()
        caption = make_detection(scan, "CASE_CAPTION", 0)
        key = make_detection(scan, "KEY_ICON", 3)
        make_boundary(scan, caption, key)
        make_detection(scan, "HEADNOTE", 0, confidence=0.95)
        make_redaction(
            scan,
            0,
            origin=Redaction.Origin.HUMAN,
            kind=Redaction.Kind.ADD,
            rect_type="manual",
        )

        findings.rebuild(scan)

        self.assertNotIn(CheckName.UNCOVERED_HEADNOTE, checks_of(scan))

    def test_the_rebuild_is_idempotent_and_leaves_review_1_alone(self):
        scan = make_scan()
        review1 = Issue.objects.create(
            scan=scan,
            check_name=CheckName.NO_PAGE_NUMBER,
            page_number=2,
            severity=Issue.Severity.WARNING,
            message="review 1",
        )
        caption = make_detection(scan, "CASE_CAPTION", 0)
        key = make_detection(scan, "KEY_ICON", 1)
        make_boundary(scan, caption, key)
        make_detection(scan, "KEY_ICON", 2)

        findings.rebuild(scan)
        first = checks_of(scan)
        findings.rebuild(scan)

        self.assertEqual(checks_of(scan), first)
        self.assertTrue(Issue.objects.filter(pk=review1.pk).exists())

    def test_the_findings_go_when_the_rows_answer_them(self):
        """The 'auto dismiss' of the issue: a finding that no longer
        holds is not written again."""
        scan = make_scan()
        caption = make_detection(scan, "CASE_CAPTION", 0)
        key = make_detection(scan, "KEY_ICON", 1)
        make_boundary(scan, caption, key)
        loose = make_detection(scan, "KEY_ICON", 2)
        findings.rebuild(scan)
        self.assertIn(CheckName.UNMATCHED_KEY_ICON, checks_of(scan))

        Detection.objects.filter(pk=loose.pk).update(active=False)
        findings.rebuild(scan)

        self.assertNotIn(CheckName.UNMATCHED_KEY_ICON, checks_of(scan))


class TestStaleFindings(ScanningTestCase):
    """A curator's row the compute could not land or place."""

    def _decision(self, scan, **fields):
        values = {
            "scan": scan,
            "kind": DetectionDecision.Kind.APPROVE,
            "source_page": 1,
            "source_fingerprint": FINGERPRINT,
            "label": "KEY_ICON",
            "label_id": 1,
            "target_x0": 100.0,
            "target_y0": 100.0,
            "target_x1": 200.0,
            "target_y1": 200.0,
        }
        values.update(fields)
        return DetectionDecision.objects.create(**values)

    def test_a_decision_no_row_points_at_is_stale(self):
        scan = make_scan()
        landed = self._decision(scan)
        make_detection(scan, "KEY_ICON", 0, decision=landed)
        lost = self._decision(scan, source_page=2)
        self._decision(scan, source_page=3, withdrawn_at=timezone.now())

        findings.rebuild(scan)

        rows = scan.issues.filter(check_name=CheckName.STALE_DETECTION_EDIT)
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.metadata["model"], "detection_decision")
        self.assertEqual(row.metadata["pk"], lost.pk)
        self.assertEqual(row.target, Issue.Target.DETECTION)
        self.assertIn("not applied", row.message)

    def test_a_decision_of_another_original_is_stale(self):
        scan = make_scan()
        old = self._decision(scan, source_fingerprint="9:9")
        make_detection(scan, "KEY_ICON", 0, decision=old)

        findings.rebuild(scan)

        self.assertEqual(checks_of(scan), [CheckName.STALE_DETECTION_EDIT])

    def test_a_hand_drawn_box_outside_the_measured_run_is_stale(self):
        scan = make_scan()
        run = ApplyRun.objects.create(
            scan=scan,
            number=1,
            built_at=timezone.now(),
            page_map={"pages": []},
            source_fingerprint=FINGERPRINT,
        )
        placed = make_detection(
            scan,
            "KEY_ICON",
            0,
            model_name=Detection.ModelName.MANUAL,
            confidence=1.0,
            apply_run=run,
        )
        unplaced = make_detection(
            scan,
            "KEY_ICON",
            1,
            model_name=Detection.ModelName.MANUAL,
            confidence=1.0,
        )

        findings.rebuild(scan, run=run)

        rows = scan.issues.filter(check_name=CheckName.STALE_DETECTION_EDIT)
        self.assertEqual([r.metadata["pk"] for r in rows], [unplaced.pk])
        self.assertNotIn(placed.pk, [r.metadata["pk"] for r in rows])
        self.assertEqual(rows.get().metadata["model"], "detection")

    def test_with_no_run_only_the_fingerprint_makes_a_box_stale(self):
        scan = make_scan()
        make_detection(
            scan,
            "KEY_ICON",
            0,
            model_name=Detection.ModelName.MANUAL,
            confidence=1.0,
        )

        findings.rebuild(scan, run=None)

        self.assertEqual(checks_of(scan), [])

    def test_an_unlanded_redaction_dismissal_is_stale(self):
        scan = make_scan()
        lost = Redaction.objects.create(
            scan=scan,
            origin=Redaction.Origin.HUMAN,
            kind=Redaction.Kind.DISMISS,
            rect_type="headnote",
            fill=Redaction.Fill.BLACK,
            source_page=1,
            page_index=0,
            source_fingerprint=FINGERPRINT,
            target_x0=1,
            target_y0=1,
            target_x1=2,
            target_y1=2,
        )
        landed = Redaction.objects.create(
            scan=scan,
            origin=Redaction.Origin.HUMAN,
            kind=Redaction.Kind.DISMISS,
            rect_type="headnote",
            fill=Redaction.Fill.BLACK,
            source_page=2,
            page_index=1,
            source_fingerprint=FINGERPRINT,
        )
        make_redaction(scan, 1, decision=landed)

        findings.rebuild(scan)

        rows = scan.issues.filter(check_name=CheckName.STALE_REDACTION_EDIT)
        self.assertEqual([r.metadata["pk"] for r in rows], [lost.pk])
        self.assertEqual(rows.get().target, Issue.Target.REDACTION)

    def test_an_unlanded_boundary_dismissal_is_stale(self):
        scan = make_scan()
        lost = OpinionBoundaryFactory(
            scan=scan,
            origin=OpinionBoundary.Origin.HUMAN,
            kind=OpinionBoundary.Kind.DISMISS,
            source_fingerprint=FINGERPRINT,
        )

        findings.rebuild(scan)

        rows = scan.issues.filter(check_name=CheckName.STALE_BOUNDARY_EDIT)
        self.assertEqual([r.metadata["pk"] for r in rows], [lost.pk])
        self.assertEqual(rows.get().target, Issue.Target.BOUNDARY)
        self.assertEqual(rows.get().page_number, 1)


class TestDismissals(ScanningTestCase):
    """A dismissal is a row at an address, resolved by the rebuild."""

    def setUp(self):
        self.user = self.make_user()
        self.scan = make_scan()
        caption = make_detection(self.scan, "CASE_CAPTION", 0)
        key = make_detection(self.scan, "KEY_ICON", 3)
        make_boundary(self.scan, caption, key)
        self.loose = make_detection(self.scan, "KEY_ICON", 1)
        findings.rebuild(self.scan)
        self.finding = self.scan.issues.get(
            check_name=CheckName.UNMATCHED_KEY_ICON
        )

    def test_dismiss_writes_the_address_and_sets_the_fk(self):
        row = findings.dismiss(self.scan, self.finding, self.user)

        self.assertEqual(row.check_name, CheckName.UNMATCHED_KEY_ICON)
        self.assertEqual(row.source_page, 2)
        self.assertEqual(row.label, "KEY_ICON")
        self.assertEqual(row.target_bbox, [100.0, 100.0, 200.0, 200.0])
        self.assertEqual(row.source_fingerprint, FINGERPRINT)
        self.assertEqual(row.author, self.user)
        self.finding.refresh_from_db()
        self.assertEqual(self.finding.dismissal, row)
        self.assertEqual(findings.open_count(self.scan), (0, 0))

    def test_a_second_dismissal_answers_the_standing_row(self):
        first = findings.dismiss(self.scan, self.finding, self.user)
        self.finding.refresh_from_db()

        second = findings.dismiss(self.scan, self.finding, self.user)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ReviewDismissal.objects.count(), 1)

    def test_the_rebuild_lands_the_dismissal_on_the_new_finding(self):
        findings.dismiss(self.scan, self.finding, self.user)

        findings.rebuild(self.scan)

        finding = self.scan.issues.get(check_name=CheckName.UNMATCHED_KEY_ICON)
        self.assertNotEqual(finding.pk, self.finding.pk)
        self.assertIsNotNone(finding.dismissal_id)
        self.assertTrue(finding.is_dismissed)

    def test_a_box_that_moved_far_is_a_new_finding(self):
        findings.dismiss(self.scan, self.finding, self.user)
        Detection.objects.filter(pk=self.loose.pk).update(x0=600.0, x1=700.0)

        findings.rebuild(self.scan)

        finding = self.scan.issues.get(check_name=CheckName.UNMATCHED_KEY_ICON)
        self.assertIsNone(finding.dismissal_id)

    def test_a_withdrawn_or_stale_dismissal_lands_on_nothing(self):
        row = findings.dismiss(self.scan, self.finding, self.user)
        findings.withdraw(ReviewDismissal.objects.filter(pk=row.pk), self.user)
        stale = findings.dismiss(
            self.scan, self.scan.issues.get(pk=self.finding.pk), self.user
        )
        ReviewDismissal.objects.filter(pk=stale.pk).update(
            source_fingerprint="9:9"
        )

        findings.rebuild(self.scan)

        finding = self.scan.issues.get(check_name=CheckName.UNMATCHED_KEY_ICON)
        self.assertIsNone(finding.dismissal_id)

    def test_restore_withdraws_and_clears(self):
        findings.dismiss(self.scan, self.finding, self.user)
        self.finding.refresh_from_db()

        self.assertTrue(findings.restore(self.scan, self.finding, self.user))

        self.finding.refresh_from_db()
        self.assertIsNone(self.finding.dismissal_id)
        row = ReviewDismissal.objects.get()
        self.assertIsNotNone(row.withdrawn_at)
        self.assertEqual(row.withdrawn_by, self.user)
        self.assertFalse(findings.restore(self.scan, self.finding, self.user))

    def test_a_finding_with_no_source_page_takes_no_dismissal(self):
        """A pre-#240 row keeps ``source_page`` blank until the next
        import; a dismissal keyed by no page could never land, and one
        written anyway would hit the NOT NULL column."""
        Detection.objects.filter(pk=self.loose.pk).update(source_page=None)
        findings.rebuild(self.scan)
        finding = self.scan.issues.get(check_name=CheckName.UNMATCHED_KEY_ICON)

        with self.assertRaises(findings.UnaddressableFinding):
            findings.dismiss(self.scan, finding, self.user)

        self.assertEqual(ReviewDismissal.objects.count(), 0)

    def test_a_stale_finding_takes_no_dismissal(self):
        DetectionDecision.objects.create(
            scan=self.scan,
            kind=DetectionDecision.Kind.APPROVE,
            source_page=1,
            source_fingerprint=FINGERPRINT,
            label="KEY_ICON",
            label_id=1,
            target_x0=1,
            target_y0=1,
            target_x1=2,
            target_y1=2,
        )
        findings.rebuild(self.scan)
        stale = self.scan.issues.get(check_name=CheckName.STALE_DETECTION_EDIT)

        with self.assertRaises(findings.UndismissableFinding):
            findings.dismiss(self.scan, stale, self.user)

    def test_withdraw_stale_takes_the_decision_back_and_rebuilds(self):
        decision = DetectionDecision.objects.create(
            scan=self.scan,
            kind=DetectionDecision.Kind.APPROVE,
            source_page=1,
            source_fingerprint=FINGERPRINT,
            label="KEY_ICON",
            label_id=1,
            target_x0=1,
            target_y0=1,
            target_x1=2,
            target_y1=2,
        )
        findings.rebuild(self.scan)
        stale = self.scan.issues.get(check_name=CheckName.STALE_DETECTION_EDIT)

        self.assertTrue(findings.withdraw_stale(self.scan, stale, self.user))

        decision.refresh_from_db()
        self.assertIsNotNone(decision.withdrawn_at)
        self.assertFalse(
            self.scan.issues.filter(
                check_name=CheckName.STALE_DETECTION_EDIT
            ).exists()
        )
        with self.assertRaises(findings.NotAStaleFinding):
            findings.withdraw_stale(
                self.scan,
                self.scan.issues.get(check_name=CheckName.UNMATCHED_KEY_ICON),
                self.user,
            )

    def test_open_count_leaves_the_dismissed_out(self):
        DetectionDecision.objects.create(
            scan=self.scan,
            kind=DetectionDecision.Kind.APPROVE,
            source_page=1,
            source_fingerprint=FINGERPRINT,
            label="KEY_ICON",
            label_id=1,
            target_x0=1,
            target_y0=1,
            target_x1=2,
            target_y1=2,
        )
        findings.rebuild(self.scan)
        self.assertEqual(findings.open_count(self.scan), (2, 1))

        findings.dismiss(
            self.scan,
            self.scan.issues.get(check_name=CheckName.UNMATCHED_KEY_ICON),
            self.user,
        )

        self.assertEqual(findings.open_count(self.scan), (1, 1))


class TestFindingEndpoints(ScanningTestCase):
    """The three decisions and the fragment, by issue id."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.scan = make_scan()
        caption = make_detection(self.scan, "CASE_CAPTION", 0)
        key = make_detection(self.scan, "KEY_ICON", 3)
        make_boundary(self.scan, caption, key)
        make_detection(self.scan, "KEY_ICON", 1)
        findings.rebuild(self.scan)
        self.finding = self.scan.issues.get(
            check_name=CheckName.UNMATCHED_KEY_ICON
        )

    def _post(self, name, body):
        return self.client.post(
            reverse(name, kwargs={"pk": self.scan.pk}),
            data=json.dumps(body),
            content_type="application/json",
        )

    def test_dismiss_and_restore(self):
        response = self._post("dismiss_finding", {"issue_id": self.finding.pk})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.finding.refresh_from_db()
        self.assertIsNotNone(self.finding.dismissal_id)

        response = self._post("restore_finding", {"issue_id": self.finding.pk})

        self.assertEqual(response.json(), {"status": "ok", "restored": True})
        self.finding.refresh_from_db()
        self.assertIsNone(self.finding.dismissal_id)

    def test_an_unknown_or_review_1_issue_is_404(self):
        review1 = Issue.objects.create(
            scan=self.scan,
            check_name=CheckName.NO_PAGE_NUMBER,
            page_number=1,
            message="x",
        )

        self.assertEqual(
            self._post("dismiss_finding", {"issue_id": 999999}).status_code,
            404,
        )
        self.assertEqual(
            self._post(
                "dismiss_finding", {"issue_id": review1.pk}
            ).status_code,
            404,
        )

    def test_a_stale_finding_is_refused_and_withdrawn(self):
        decision = DetectionDecision.objects.create(
            scan=self.scan,
            kind=DetectionDecision.Kind.APPROVE,
            source_page=1,
            source_fingerprint=FINGERPRINT,
            label="KEY_ICON",
            label_id=1,
            target_x0=1,
            target_y0=1,
            target_x1=2,
            target_y1=2,
        )
        findings.rebuild(self.scan)
        stale = self.scan.issues.get(check_name=CheckName.STALE_DETECTION_EDIT)

        response = self._post("dismiss_finding", {"issue_id": stale.pk})
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["status"], "error")

        response = self._post("withdraw_stale_edit", {"issue_id": stale.pk})
        self.assertEqual(response.json(), {"status": "ok", "withdrawn": True})
        decision.refresh_from_db()
        self.assertIsNotNone(decision.withdrawn_at)

        # The rebuild replaced the rows, so the unmatched card is read
        # again; a withdrawal through a card that names no row is refused.
        unmatched = self.scan.issues.get(
            check_name=CheckName.UNMATCHED_KEY_ICON
        )
        response = self._post(
            "withdraw_stale_edit", {"issue_id": unmatched.pk}
        )
        self.assertEqual(response.status_code, 409)

    def test_a_finding_with_no_address_is_refused_with_409(self):
        Detection.objects.filter(
            pk=self.finding.metadata["detection_id"]
        ).update(source_page=None)
        findings.rebuild(self.scan)
        finding = self.scan.issues.get(check_name=CheckName.UNMATCHED_KEY_ICON)

        response = self._post("dismiss_finding", {"issue_id": finding.pk})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["status"], "error")

    def test_the_fragment_renders_the_cards(self):
        response = self.client.get(
            reverse("review_findings", kwargs={"pk": self.scan.pk})
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["open"], 1)
        self.assertEqual(data["stale"], 0)
        self.assertIn(f'data-issue-id="{self.finding.pk}"', data["html"])
        self.assertIn("dismissFinding(", data["html"])
        self.assertIn(
            f'data-detection-id="{self.finding.metadata["detection_id"]}"',
            data["html"],
        )

    def test_login_is_required(self):
        self.client.logout()

        response = self._post("dismiss_finding", {"issue_id": self.finding.pk})

        self.assertEqual(response.status_code, 302)

    def test_a_detection_write_rebuilds_the_findings(self):
        """The endpoints keep the cards true (#240 PR D): deleting the
        loose key icon takes its card away in the same request."""
        loose = Detection.objects.get(pk=self.finding.metadata["detection_id"])

        response = self._post("delete_detection", {"detection_id": loose.pk})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            self.scan.issues.filter(
                check_name=CheckName.UNMATCHED_KEY_ICON
            ).exists()
        )

    def test_review_1_dismiss_refuses_a_review_2_finding(self):
        response = self._post("dismiss_issue", {"issue_id": self.finding.pk})

        self.assertEqual(response.status_code, 409)
        self.assertTrue(Issue.objects.filter(pk=self.finding.pk).exists())
        self.assertFalse(
            PageEdit.objects.filter(kind=PageEdit.Kind.DISMISS_ISSUE).exists()
        )


class TestTheView(ScanningTestCase):
    """Two lists: review 1 in step 1, review 2 in step 2."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.scan = make_scan(
            page_map=[
                {"pdf_index": i, "logical_number": 101 + i, "type": "pdf_page"}
                for i in range(4)
            ],
            ocr_results=[
                {"pdf_page": i + 1, "detected": str(101 + i), "type": "single"}
                for i in range(4)
            ],
        )
        caption = make_detection(self.scan, "CASE_CAPTION", 0)
        key = make_detection(self.scan, "KEY_ICON", 1)
        make_boundary(self.scan, caption, key)
        make_detection(self.scan, "KEY_ICON", 2)
        Issue.objects.create(
            scan=self.scan,
            check_name=CheckName.NO_PAGE_NUMBER,
            page_number=2,
            severity=Issue.Severity.WARNING,
            message="review 1",
        )
        findings.rebuild(self.scan)

    def _page(self, step):
        response = self.client.get(
            reverse("scan_process", kwargs={"pk": self.scan.pk}),
            {"step": step},
        )
        self.assertEqual(response.status_code, 200)
        return response

    def test_step_1_lists_the_review_1_rows_only(self):
        response = self._page(1)

        checks = {i.check_name for i in response.context["issues"]}
        self.assertEqual(checks, {CheckName.NO_PAGE_NUMBER})

    def test_step_2_lists_the_findings_by_target(self):
        response = self._page(2)

        groups = response.context["finding_groups"]
        self.assertEqual(
            [g["key"] for g in groups],
            [Issue.Target.DETECTION, Issue.Target.PAGES],
        )
        key_card = groups[0]["rows"][0]
        self.assertEqual(key_card.nav_pdf_index, 2)
        self.assertEqual(key_card.logical_page, 103)
        self.assertEqual(response.context["review2_open"], 2)
        html = response.content.decode()
        self.assertIn('id="review-findings"', html)
        self.assertNotIn("Unmatched Key Icons", html)

    def test_the_approve_button_asks_for_a_confirm_with_open_findings(self):
        response = self.client.get(
            reverse("process_actions", kwargs={"pk": self.scan.pk}),
            {"step": 2},
        )
        html = response.json()["html"]

        self.assertIn("2 findings open", html)
        self.assertIn("Approve the redactions anyway?", html)

        Issue.objects.filter(
            scan=self.scan, check_name__in=REVIEW2_CHECKS
        ).delete()
        html = self.client.get(
            reverse("process_actions", kwargs={"pk": self.scan.pk}),
            {"step": 2},
        ).json()["html"]

        self.assertNotIn("Approve the redactions anyway?", html)
        self.assertIn("approve-redactions", html)

    def test_the_approval_obeys(self):
        response = self.client.post(
            reverse("approve_redaction_review", kwargs={"pk": self.scan.pk})
        )

        self.assertEqual(response.status_code, 302)
        self.scan.refresh_from_db()
        self.assertEqual(self.scan.status, Status.REDACTION_REVIEW_DONE)


class TestTheCommand(ScanningTestCase):
    """The backfill after the deploy."""

    def test_it_rebuilds_the_open_reviews(self):
        scan = make_scan()
        caption = make_detection(scan, "CASE_CAPTION", 0)
        key = make_detection(scan, "KEY_ICON", 1)
        make_boundary(scan, caption, key)
        closed = make_scan(status=Status.REDACTION_REVIEW_DONE)
        c2 = make_detection(closed, "CASE_CAPTION", 0)
        k2 = make_detection(closed, "KEY_ICON", 1)
        make_boundary(closed, c2, k2)

        out = StringIO()
        call_command("rebuild_review2_findings", "--dry-run", stdout=out)
        self.assertEqual(checks_of(scan), [])

        call_command("rebuild_review2_findings", stdout=out)
        self.assertEqual(checks_of(scan), [CheckName.UNCOVERED_PAGES])
        self.assertEqual(checks_of(closed), [])

        call_command("rebuild_review2_findings", "--all", stdout=out)
        self.assertEqual(checks_of(closed), [CheckName.UNCOVERED_PAGES])
