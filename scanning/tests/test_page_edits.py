"""Tests for the one model that holds a human page edit (issue #214).

This module covers the storage and the address space: the constraints
that keep one decision at one address, and the image key. The readers
that overlay these rows live in ``test_services.py`` and
``test_page_numbers.py``; the endpoints that write them live in
``test_views.py``.
"""

import json
import tempfile

from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from scanning.factories import PageEditFactory, ScanFactory, UserFactory
from scanning.models import CheckName, Issue, PageEdit, Status

MEDIA_ROOT = tempfile.mkdtemp()


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestPageEditConstraints(TestCase):
    """The database keeps one open decision per address."""

    def setUp(self):
        self.scan = ScanFactory()

    def _refused(self, **kwargs):
        """Assert the database refuses one PageEdit.

        :param kwargs: Field values for ``PageEditFactory``.
        """
        with self.assertRaises(IntegrityError), transaction.atomic():
            PageEditFactory(scan=self.scan, **kwargs)

    def test_an_insert_lives_in_a_gap(self):
        edit = PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.INSERT_PAGE,
            pdf_page=None,
            anchor_pdf_page=0,
            value="",
        )
        self.assertEqual(edit.anchor_pdf_page, 0)

    def test_an_insert_without_an_anchor_is_refused(self):
        self._refused(
            kind=PageEdit.Kind.INSERT_PAGE,
            pdf_page=None,
            anchor_pdf_page=None,
            value="",
        )

    def test_an_insert_with_a_page_address_is_refused(self):
        self._refused(
            kind=PageEdit.Kind.INSERT_PAGE,
            pdf_page=4,
            anchor_pdf_page=3,
            value="",
        )

    def test_a_page_kind_without_a_page_is_refused(self):
        self._refused(kind=PageEdit.Kind.DELETE_PAGE, pdf_page=None)

    def test_a_page_kind_with_an_anchor_is_refused(self):
        self._refused(
            kind=PageEdit.Kind.DELETE_PAGE,
            pdf_page=4,
            anchor_pdf_page=3,
        )

    def test_a_dismissal_names_its_check(self):
        self._refused(kind=PageEdit.Kind.DISMISS_ISSUE, value="")

    def test_a_dismissal_may_name_the_volume(self):
        edit = PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.DISMISS_ISSUE,
            pdf_page=None,
            value="mislabeled_document",
        )
        self.assertIsNone(edit.pdf_page)

    def test_a_rotation_is_a_quarter_turn(self):
        PageEditFactory(
            scan=self.scan, kind=PageEdit.Kind.ROTATE_PAGE, value="180"
        )
        self._refused(kind=PageEdit.Kind.ROTATE_PAGE, pdf_page=2, value="45")

    def test_one_open_decision_per_page_and_kind(self):
        PageEditFactory(scan=self.scan, pdf_page=7, value="700")
        self._refused(pdf_page=7, value="701")

    def test_two_kinds_may_share_a_page(self):
        PageEditFactory(scan=self.scan, pdf_page=7, value="700")
        PageEditFactory(
            scan=self.scan, kind=PageEdit.Kind.DELETE_PAGE, pdf_page=7
        )
        self.assertEqual(self.scan.page_edits.count(), 2)

    def test_two_scans_may_share_a_page(self):
        other = ScanFactory()
        PageEditFactory(scan=self.scan, pdf_page=7, value="700")
        PageEditFactory(scan=other, pdf_page=7, value="700")
        self.assertEqual(PageEdit.objects.count(), 2)

    def test_an_applied_decision_frees_its_address(self):
        # The apply closes a row and produces a new original. A curator
        # editing the same page again writes a new row, so the unique
        # key must count only the open ones.
        PageEditFactory(
            scan=self.scan,
            pdf_page=7,
            value="700",
            applied_at=timezone.now(),
        )
        again = PageEditFactory(scan=self.scan, pdf_page=7, value="701")
        self.assertIsNone(again.applied_at)
        self.assertEqual(self.scan.page_edits.count(), 2)

    def test_one_dismissal_per_check_not_per_page(self):
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.DISMISS_ISSUE,
            pdf_page=7,
            value="duplicate_page",
        )
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.DISMISS_ISSUE,
            pdf_page=7,
            value="blank_page",
        )
        self._refused(
            kind=PageEdit.Kind.DISMISS_ISSUE,
            pdf_page=7,
            value="blank_page",
        )

    def test_a_dismissal_may_name_a_printed_page(self):
        # An issue names a page in one of two spaces. A missing-page
        # warning names the printed number, which has no physical page
        # to point at: that is the whole reason it was raised.
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.DISMISS_ISSUE,
            pdf_page=None,
            logical_page="1074",
            value="missing_page",
        )
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.DISMISS_ISSUE,
            pdf_page=None,
            logical_page="1080",
            value="missing_page",
        )
        self._refused(
            kind=PageEdit.Kind.DISMISS_ISSUE,
            pdf_page=None,
            logical_page="1080",
            value="missing_page",
        )

    def test_one_volume_dismissal_per_check(self):
        # Two null addresses are the same address here, which is what
        # ``nulls_distinct=False`` buys.
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.DISMISS_ISSUE,
            pdf_page=None,
            value="mislabeled_document",
        )
        self._refused(
            kind=PageEdit.Kind.DISMISS_ISSUE,
            pdf_page=None,
            value="mislabeled_document",
        )

    def test_one_insert_per_gap_and_ordinal(self):
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.INSERT_PAGE,
            pdf_page=None,
            anchor_pdf_page=3,
            value="",
        )
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.INSERT_PAGE,
            pdf_page=None,
            anchor_pdf_page=3,
            ordinal=1,
            value="",
        )
        self._refused(
            kind=PageEdit.Kind.INSERT_PAGE,
            pdf_page=None,
            anchor_pdf_page=3,
            ordinal=1,
            value="",
        )


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestPageEditImageKey(TestCase):
    """The image key is the scan's own, under ``page_edits/``."""

    def test_the_key_sits_under_the_scan_prefix(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from scanning import s3_sync

        scan = ScanFactory()
        edit = PageEditFactory(
            scan=scan,
            kind=PageEdit.Kind.REPLACE_PAGE,
            pdf_page=4,
            value="",
            image=SimpleUploadedFile(
                "page.png", b"not-a-real-png", content_type="image/png"
            ),
        )
        expected = (
            f"{s3_sync.s3_processing_prefix(scan)}{s3_sync.PAGE_EDITS_SUBDIR}"
        )
        self.assertTrue(edit.image.name.startswith(expected))
        self.assertTrue(edit.image.name.endswith(".png"))

    def test_the_key_is_excluded_from_the_generic_sync(self):
        from scanning import s3_sync

        self.assertFalse(s3_sync._is_synced_by_default("page_edits/abc.png"))


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestAssignPageWritesAnEdit(TestCase):
    """``views_process.assign_page`` records a decision, not a blob edit."""

    def setUp(self):
        self.user = UserFactory()
        self.client.force_login(self.user)
        self.scan = ScanFactory(
            status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
            page_count=2,
            source_fingerprint="100:2",
            ocr_results=[
                {
                    "pdf_page": 1,
                    "detected": "5",
                    "type": "single",
                    "zone": "dots-header",
                },
                {
                    "pdf_page": 2,
                    "detected": None,
                    "type": None,
                    "zone": None,
                },
            ],
        )

    def _post(self, pdf_page, page_number):
        """POST one page number to the view.

        :param pdf_page: 1-based PDF page.
        :param page_number: The value a curator typed.
        :returns: The response.
        """
        return self.client.post(
            reverse("assign_page", kwargs={"pk": self.scan.pk}),
            data=json.dumps(
                {"pdf_page": pdf_page, "page_number": page_number}
            ),
            content_type="application/json",
        )

    def test_a_number_becomes_one_row(self):
        self.assertEqual(self._post(2, "6").status_code, 200)

        edit = self.scan.page_edits.get()
        self.assertEqual(edit.kind, PageEdit.Kind.SET_NUMBER)
        self.assertEqual(edit.pdf_page, 2)
        self.assertEqual(edit.value, "6")
        self.assertEqual(edit.author, self.user)
        self.assertEqual(edit.source_fingerprint, "100:2")
        self.assertIsNone(edit.applied_at)

    def test_the_row_records_what_it_overruled(self):
        self._post(1, "6")

        edit = self.scan.page_edits.get()
        self.assertEqual(edit.replaced, "5")

    def test_a_second_edit_of_one_page_updates_its_row(self):
        # Two curators on two pages was the lost update this model
        # removes; one curator changing their mind is still one row.
        self._post(2, "6")
        self._post(2, "7")

        edit = self.scan.page_edits.get()
        self.assertEqual(edit.value, "7")

    def test_two_curators_keep_both_numbers(self):
        # The defect in the blob: the second full-list write dropped
        # the first curator's entry, with no error and no trace.
        self._post(1, "6")
        self._post(2, "7")

        self.scan.refresh_from_db()
        self.assertEqual(self.scan.page_edits.count(), 2)
        self.assertEqual(
            [r["detected"] for r in self.scan.ocr_results], ["6", "7"]
        )

    def test_a_range_is_accepted(self):
        self.assertEqual(self._post(2, "678-686").status_code, 200)

        self.assertEqual(self.scan.page_edits.get().value, "678-686")
        self.scan.refresh_from_db()
        self.assertEqual(self.scan.ocr_results[1]["type"], "range")

    def test_a_backwards_range_is_refused(self):
        self.assertEqual(self._post(2, "686-678").status_code, 400)
        self.assertFalse(self.scan.page_edits.exists())

    def test_a_blank_value_is_a_decision_too(self):
        self.assertEqual(self._post(1, "").status_code, 200)

        edit = self.scan.page_edits.get()
        self.assertEqual(edit.kind, PageEdit.Kind.SET_NUMBER)
        self.assertEqual(edit.value, "")
        self.scan.refresh_from_db()
        self.assertIsNone(self.scan.ocr_results[0]["detected"])

    def test_an_unknown_page_writes_nothing(self):
        self.assertEqual(self._post(99, "6").status_code, 404)
        self.assertFalse(self.scan.page_edits.exists())


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestDismissIssueWritesAnEdit(TestCase):
    """A dismissal is a decision, so it survives the rebuild."""

    def setUp(self):
        self.user = UserFactory()
        self.client.force_login(self.user)
        self.scan = ScanFactory(
            status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
            start_page=1,
            end_page=2,
            page_count=2,
            source_fingerprint="100:2",
            ocr_results=[
                {"pdf_page": 1, "detected": None, "type": None, "zone": None},
                {
                    "pdf_page": 2,
                    "detected": "2",
                    "type": "single",
                    "zone": "dots-header",
                },
            ],
        )

    def _dismiss(self, issue):
        """POST the dismissal of one issue.

        :param issue: The Issue row to dismiss.
        :returns: The response.
        """
        return self.client.post(
            reverse("dismiss_issue", kwargs={"pk": self.scan.pk}),
            data=json.dumps({"issue_id": issue.pk}),
            content_type="application/json",
        )

    def test_a_physical_check_keeps_a_physical_address(self):
        issue = Issue.objects.create(
            scan=self.scan,
            page_number=1,
            check_name=CheckName.NO_PAGE_NUMBER,
            message="no number",
        )

        self.assertEqual(self._dismiss(issue).status_code, 200)

        edit = self.scan.page_edits.get()
        self.assertEqual(edit.kind, PageEdit.Kind.DISMISS_ISSUE)
        self.assertEqual(edit.value, CheckName.NO_PAGE_NUMBER)
        self.assertEqual(edit.pdf_page, 1)
        self.assertEqual(edit.logical_page, "")
        self.assertFalse(Issue.objects.filter(pk=issue.pk).exists())

    def test_a_logical_check_keeps_a_printed_address(self):
        issue = Issue.objects.create(
            scan=self.scan,
            page_number=1074,
            check_name=CheckName.MISSING_PAGE,
            message="missing",
        )

        self._dismiss(issue)

        edit = self.scan.page_edits.get()
        self.assertIsNone(edit.pdf_page)
        self.assertEqual(edit.logical_page, "1074")

    def test_a_dismissal_survives_a_recompute(self):
        # The rebuild deletes every derived issue and writes new rows,
        # so a dismissal that was a deleted row came straight back.
        from scanning import services

        services.recalculate_issues(self.scan)
        issue = self.scan.issues.get(check_name=CheckName.NO_PAGE_NUMBER)
        self._dismiss(issue)

        services.recalculate_issues(self.scan)

        self.assertFalse(
            self.scan.issues.filter(
                check_name=CheckName.NO_PAGE_NUMBER
            ).exists()
        )

    def test_an_unknown_issue_is_a_404(self):
        self.assertEqual(
            self.client.post(
                reverse("dismiss_issue", kwargs={"pk": self.scan.pk}),
                data=json.dumps({"issue_id": 9999}),
                content_type="application/json",
            ).status_code,
            404,
        )


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestManualReadingMigration(TestCase):
    """The #214 data migration, run against the live app registry.

    The historical models it asks for are these models, so the real
    registry answers the same question the migration asks.
    """

    def _migrate(self):
        """Run the migration's forward function once."""
        from importlib import import_module

        from django.apps import apps as live_apps

        migration = import_module(
            "scanning.migrations.0013_page_edits_from_manual_readings"
        )
        migration.create_page_edits(live_apps, None)

    def test_a_manual_reading_becomes_a_row(self):
        scan = ScanFactory(
            ocr_results=[
                {"pdf_page": 1, "detected": "5", "zone": "dots-header"},
                {
                    "pdf_page": 2,
                    "detected": "9",
                    "zone": "manual",
                    "ocr": "manual",
                },
            ]
        )

        self._migrate()

        edit = scan.page_edits.get()
        self.assertEqual(edit.kind, PageEdit.Kind.SET_NUMBER)
        self.assertEqual(edit.pdf_page, 2)
        self.assertEqual(edit.value, "9")
        self.assertIsNone(edit.author)
        self.assertEqual(edit.source_fingerprint, "")

    def test_a_cleared_reading_becomes_a_blank_row(self):
        scan = ScanFactory(
            ocr_results=[
                {
                    "pdf_page": 1,
                    "detected": None,
                    "zone": "manual",
                    "ocr": "manual",
                }
            ]
        )

        self._migrate()

        self.assertEqual(scan.page_edits.get().value, "")

    def test_running_it_twice_writes_one_row(self):
        scan = ScanFactory(
            ocr_results=[
                {
                    "pdf_page": 1,
                    "detected": "9",
                    "zone": "manual",
                    "ocr": "manual",
                }
            ]
        )

        self._migrate()
        self._migrate()

        self.assertEqual(scan.page_edits.count(), 1)
