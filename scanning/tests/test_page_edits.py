"""Tests for the one model that holds a human page edit (issue #214).

This module covers the storage and the address space: the constraints
that keep one decision at one address, and the image key. The readers
that overlay these rows live in ``test_services.py`` and
``test_page_numbers.py``; the endpoints that write them live in
``test_views.py``.
"""

import json
import pathlib
import re
import tempfile
from unittest import mock

import fitz
from django.conf import settings
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import (
    SimpleUploadedFile,
    TemporaryUploadedFile,
)
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from scanning import page_edits, page_numbers, s3_sync, views_process
from scanning.factories import PageEditFactory, ScanFactory, UserFactory
from scanning.models import CheckName, Issue, PageEdit, Scan, Status
from scanning.tests.test_sharding import write_image_volume
from scanning.tests.test_views import ScanningTestCase

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

    def test_a_move_names_its_page_and_its_gap(self):
        # #261: the page that moves, and the original page it follows.
        edit = PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.MOVE_PAGE,
            pdf_page=886,
            anchor_pdf_page=884,
            value="",
        )
        self.assertEqual(
            str(edit), "Move a page to another place p.886 to after p.884"
        )
        self._refused(
            kind=PageEdit.Kind.MOVE_PAGE,
            pdf_page=886,
            anchor_pdf_page=None,
            value="",
        )
        self._refused(
            kind=PageEdit.Kind.MOVE_PAGE,
            pdf_page=None,
            anchor_pdf_page=884,
            value="",
        )

    def test_a_move_may_go_before_page_1_but_not_after_itself(self):
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.MOVE_PAGE,
            pdf_page=3,
            anchor_pdf_page=0,
            value="",
        )
        self._refused(
            kind=PageEdit.Kind.MOVE_PAGE,
            pdf_page=4,
            anchor_pdf_page=4,
            value="",
        )

    def test_one_standing_move_per_page(self):
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.MOVE_PAGE,
            pdf_page=3,
            anchor_pdf_page=0,
            value="",
        )
        self._refused(
            kind=PageEdit.Kind.MOVE_PAGE,
            pdf_page=3,
            anchor_pdf_page=5,
            value="",
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

    def test_an_applied_decision_keeps_its_address(self):
        # The apply stamp is a ledger entry, not a close (#224): an
        # applied row stands, so the address is still taken. A curator
        # who decides again supersedes it (``page_edits.supersede``),
        # which withdraws the applied row first.
        PageEditFactory(
            scan=self.scan,
            pdf_page=7,
            value="700",
            applied_at=timezone.now(),
        )
        self._refused(pdf_page=7, value="701")

    def test_a_withdrawn_decision_frees_its_address(self):
        PageEditFactory(
            scan=self.scan,
            pdf_page=7,
            value="700",
            applied_at=timezone.now(),
            withdrawn_at=timezone.now(),
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

    def test_the_row_records_the_reading_it_overruled(self):
        self._post(1, "6")

        edit = self.scan.page_edits.get()
        self.assertEqual(edit.previous_value, "5")

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

    def test_a_dismissed_auto_correction_stays_dismissed(self):
        # The auto-correction warnings are appended after the checks
        # the analysis produced, so the filter must run over the whole
        # list. The apply pass rewrites ocr_results from the run on
        # every tick, so the heuristic -- and its warning -- come back
        # each time until a curator's decision stops them.
        from scanning import services

        raw = [
            {
                "pdf_page": 1,
                "detected": "100",
                "type": "single",
                "zone": "dots-header",
            },
            {
                "pdf_page": 2,
                "detected": "5",
                "type": "single",
                "zone": "dots-header",
            },
            {
                "pdf_page": 3,
                "detected": "102",
                "type": "single",
                "zone": "dots-header",
            },
        ]
        Scan.objects.filter(pk=self.scan.pk).update(
            start_page=100, end_page=110, page_count=3, ocr_results=raw
        )
        self.scan.refresh_from_db()

        services.recalculate_issues(self.scan)
        self._dismiss(
            self.scan.issues.get(check_name=CheckName.AUTO_CORRECTED)
        )

        Scan.objects.filter(pk=self.scan.pk).update(ocr_results=raw)
        self.scan.refresh_from_db()
        services.recalculate_issues(self.scan)

        self.assertFalse(
            self.scan.issues.filter(
                check_name=CheckName.AUTO_CORRECTED
            ).exists()
        )

    def test_a_dismissed_stale_edit_warning_stays_dismissed(self):
        from scanning import services

        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.SET_NUMBER,
            pdf_page=9,
            value="9",
        )
        services.recalculate_issues(self.scan)
        self._dismiss(
            self.scan.issues.get(check_name=CheckName.STALE_PAGE_EDIT)
        )

        services.recalculate_issues(self.scan)

        self.assertFalse(
            self.scan.issues.filter(
                check_name=CheckName.STALE_PAGE_EDIT
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


class TestADeletionAnswersItsCards(TestCase):
    """A card about a page marked for deletion goes away (#255)."""

    def setUp(self):
        self.user = UserFactory()
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

    def _delete_page_1(self, **fields):
        """Mark PDF page 1 for deletion.

        :param fields: Values that overwrite the row's defaults.
        :returns: The new edit.
        :rtype: PageEdit
        """
        defaults = {
            "kind": PageEdit.Kind.DELETE_PAGE,
            "pdf_page": 1,
            "value": "",
            "source_fingerprint": self.scan.source_fingerprint,
        }
        return PageEditFactory(scan=self.scan, **{**defaults, **fields})

    def _checks(self):
        """Read the checks the rebuild wrote.

        :returns: One name per issue row of the scan.
        :rtype: list[str]
        """
        from scanning import services

        services.recalculate_issues(self.scan)
        return list(
            self.scan.issues.values_list("check_name", flat=True).order_by(
                "check_name"
            )
        )

    def test_the_card_of_a_deleted_page_goes(self):
        self._delete_page_1()

        self.assertNotIn(CheckName.NO_PAGE_NUMBER, self._checks())

    def test_the_card_of_a_live_page_stays(self):
        # The same volume, with no deletion on it.
        self.assertIn(CheckName.NO_PAGE_NUMBER, self._checks())

    def test_a_printed_number_card_stays(self):
        # "Page 1 is missing" names the printed number 1, not PDF page
        # 1. To answer it the sequence analysis must run again over the
        # volume without the deleted pages, which this pass does not do.
        self._delete_page_1()

        self.assertIn(CheckName.MISSING_PAGE, self._checks())

    def test_an_undone_deletion_brings_the_card_back(self):
        edit = self._delete_page_1()
        self.assertNotIn(CheckName.NO_PAGE_NUMBER, self._checks())

        page_edits.withdraw(PageEdit.objects.filter(pk=edit.pk), self.user)

        self.assertIn(CheckName.NO_PAGE_NUMBER, self._checks())

    def test_a_deletion_of_another_original_hides_nothing(self):
        # The row names a page nobody chose, so it answers no card and
        # it keeps the warning that says it did not land.
        self._delete_page_1(source_fingerprint="999:9")

        checks = self._checks()
        self.assertIn(CheckName.NO_PAGE_NUMBER, checks)
        self.assertIn(CheckName.STALE_PAGE_EDIT, checks)


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


class TestDeletePageWritesAnEdit(ScanningTestCase):
    """``delete_page`` and its undo, over the rows."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.scan = ScanFactory(page_count=4, source_fingerprint="100:4")

    def _post(self, name, pdf_page):
        """POST one page to a page-scoped endpoint.

        :param name: The URL name.
        :param pdf_page: The page to send.
        :returns: The response.
        """
        return self.client.post(
            reverse(name, kwargs={"pk": self.scan.pk}),
            data=json.dumps({"pdf_page": pdf_page}),
            content_type="application/json",
        )

    def test_a_deletion_becomes_one_row(self):
        self.assertEqual(self._post("delete_page", 2).status_code, 200)

        edit = self.scan.page_edits.get()
        self.assertEqual(edit.kind, PageEdit.Kind.DELETE_PAGE)
        self.assertEqual(edit.pdf_page, 2)
        self.assertEqual(edit.author, self.user)
        self.assertEqual(edit.source_fingerprint, "100:4")

    def test_a_second_press_changes_nothing(self):
        self._post("delete_page", 2)
        self._post("delete_page", 2)
        self.assertEqual(self.scan.page_edits.count(), 1)

    def test_an_undo_withdraws_the_row_and_keeps_it(self):
        self._post("delete_page", 2)

        self.assertEqual(self._post("undo_delete_page", 2).status_code, 200)

        edit = self.scan.page_edits.get()
        self.assertIsNotNone(edit.withdrawn_at)
        self.assertEqual(edit.withdrawn_by, self.user)
        # ``update`` skips ``auto_now``; the audit reads the last touch.
        self.assertEqual(edit.date_modified, edit.withdrawn_at)
        self.assertEqual(page_edits.deleted_pages(self.scan), set())

    def test_a_page_can_be_deleted_again_after_an_undo(self):
        self._post("delete_page", 2)
        self._post("undo_delete_page", 2)

        self.assertEqual(self._post("delete_page", 2).status_code, 200)

        self.assertEqual(self.scan.page_edits.count(), 2)
        self.assertEqual(page_edits.deleted_pages(self.scan), {2})

    def test_a_page_the_volume_does_not_have_is_refused(self):
        self.assertEqual(self._post("delete_page", 9).status_code, 404)
        self.assertFalse(self.scan.page_edits.exists())

    def _post_pages(self, pages):
        return self.client.post(
            reverse("delete_page", kwargs={"pk": self.scan.pk}),
            data=json.dumps({"pdf_pages": pages}),
            content_type="application/json",
        )

    def test_several_pages_are_marked_in_one_request(self):
        response = self._post_pages([1, 2, 3])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["pdf_pages"], [1, 2, 3])
        self.assertEqual(page_edits.deleted_pages(self.scan), {1, 2, 3})
        self.assertTrue(
            all(e.author == self.user for e in self.scan.page_edits.all())
        )

    def test_one_bad_page_refuses_the_whole_request(self):
        self.assertEqual(self._post_pages([1, 2, 9]).status_code, 404)
        self.assertFalse(self.scan.page_edits.exists())

    def test_an_empty_list_is_refused(self):
        self.assertEqual(self._post_pages([]).status_code, 404)


class TestFrontMatterCard(TestCase):
    """The unnumbered run at the front of the volume is one card."""

    def _scan(self, detected, deleted=()):
        """A scan whose pages read as ``detected``, some marked."""
        scan = ScanFactory(
            status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
            start_page=1,
            end_page=len(detected),
            page_count=len(detected),
            source_fingerprint=f"100:{len(detected)}",
            ocr_results=[
                {
                    "pdf_page": i,
                    "detected": d,
                    "type": "single" if d else None,
                    "zone": "dots-header" if d else None,
                }
                for i, d in enumerate(detected, 1)
            ],
        )
        for page in deleted:
            PageEditFactory(
                scan=scan,
                kind=PageEdit.Kind.DELETE_PAGE,
                pdf_page=page,
                value="",
                source_fingerprint=scan.source_fingerprint,
            )
        return scan

    def _card(self, scan):
        from scanning import services

        services.recalculate_issues(scan)
        return scan.issues.filter(check_name=CheckName.FRONT_MATTER).first()

    def test_the_leading_run_is_one_card_naming_every_page(self):
        card = self._card(self._scan([None, None, None, "1", "2"]))
        self.assertIsNotNone(card)
        self.assertEqual(card.page_number, 1)
        self.assertEqual(card.severity, "warning")
        self.assertIn("PDF pages 1-3", card.message)
        self.assertTrue(card.message.endswith("[1, 2, 3]"))

    def test_an_unnumbered_run_later_in_the_volume_gets_no_card(self):
        self.assertIsNone(self._card(self._scan(["1", None, None, "4"])))

    def test_a_volume_with_no_number_at_all_gets_no_card(self):
        self.assertIsNone(self._card(self._scan([None, None, None])))

    def test_marked_pages_are_skipped_and_the_card_shrinks(self):
        card = self._card(self._scan([None, None, None, "1"], deleted=(1, 2)))
        self.assertEqual(card.page_number, 3)
        self.assertIn("PDF page 3", card.message)
        self.assertTrue(card.message.endswith("[3]"))

    def test_the_card_goes_when_the_run_is_marked(self):
        self.assertIsNone(
            self._card(self._scan([None, None, "1"], deleted=(1, 2)))
        )

    def test_one_no_page_number_card_per_page_still_stands(self):
        from scanning import services

        scan = self._scan([None, None, "1"])
        services.recalculate_issues(scan)
        self.assertEqual(
            scan.issues.filter(check_name=CheckName.NO_PAGE_NUMBER).count(), 2
        )


class TestPageInsertEndpoints(ScanningTestCase):
    """An insert is addressed by the gap it fills, and can be taken back."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.scan = ScanFactory(
            page_count=2,
            source_fingerprint="100:2",
            page_map=[
                {"type": "pdf_page", "pdf_index": 0, "logical_number": 1},
                {"type": "missing", "logical_number": 2},
                {"type": "pdf_page", "pdf_index": 1, "logical_number": 3},
            ],
        )

    def _upload(self, **fields):
        """POST one image to ``add_page_insert``.

        :param fields: Form fields beside the image.
        :returns: The response.
        """
        return self.client.post(
            reverse("add_page_insert", kwargs={"pk": self.scan.pk}),
            data={"image": self.make_image(), **fields},
        )

    def test_the_anchor_the_viewer_sends_is_stored(self):
        response = self._upload(anchor_pdf_page=1, page_number=2)

        self.assertEqual(response.status_code, 200)
        edit = self.scan.page_edits.get()
        self.assertEqual(edit.kind, PageEdit.Kind.INSERT_PAGE)
        self.assertEqual(edit.anchor_pdf_page, 1)
        self.assertEqual(edit.ordinal, 0)
        self.assertEqual(edit.logical_page, "2")
        self.assertIsNone(edit.pdf_page)
        self.assertTrue(edit.image.name.endswith(".png"))

    def test_a_second_image_in_one_gap_queues_behind_the_first(self):
        self._upload(anchor_pdf_page=1, page_number=2)
        self._upload(anchor_pdf_page=1, page_number=2)

        self.assertEqual(
            sorted(self.scan.page_edits.values_list("ordinal", flat=True)),
            [0, 1],
        )

    def test_an_image_can_go_before_page_one(self):
        self._upload(anchor_pdf_page=0, page_number=1)

        self.assertEqual(self.scan.page_edits.get().anchor_pdf_page, 0)

    def test_an_older_viewer_is_placed_from_the_page_map(self):
        # No anchor in the form: resolve the placeholder by its printed
        # number, the address the retired PageInsert model used.
        response = self._upload(page_number=2)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.scan.page_edits.get().anchor_pdf_page, 1)

    def test_a_range_missing_at_the_end_takes_one_upload(self):
        # The placeholder of a collapsed trailing run is labelled with
        # the range it stands for (#256), and an insert may be several
        # pages, so one PDF of the whole range fills it.
        self.scan.page_map = self.scan.page_map + [
            {
                "type": "missing",
                "logical_number": "4-13",
                "missing_range": [4, 13],
            }
        ]
        self.scan.save(update_fields=["page_map"])

        response = self._upload(anchor_pdf_page=2, page_number="4-13")

        self.assertEqual(response.status_code, 200)
        edit = self.scan.page_edits.get()
        self.assertEqual(edit.anchor_pdf_page, 2)
        self.assertEqual(edit.logical_page, "4-13")

    def test_a_printed_number_may_hold_letters(self):
        # A printed page number is not always a whole number: front
        # matter prints roman numerals, and an inserted leaf prints a
        # letter suffix. Casting the label to an integer would lose
        # what the curator read off the page.
        for label in ("xiv", "1075a", "A-3"):
            with self.subTest(label=label):
                self.scan.page_edits.all().delete()
                response = self._upload(anchor_pdf_page=1, page_number=label)

                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    self.scan.page_edits.get().logical_page, label
                )

    def test_a_label_carrying_markup_is_refused(self):
        # The label is a person's typing, and every viewer of the scan
        # sees it. The viewer escapes it where it draws it; the column
        # never takes it in the first place.
        response = self._upload(
            anchor_pdf_page=1,
            page_number="<img src=x onerror=alert(1)>",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.scan.page_edits.exists())

    def test_a_label_longer_than_the_column_is_refused(self):
        response = self._upload(anchor_pdf_page=1, page_number="9" * 33)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.scan.page_edits.exists())

    def test_a_page_that_prints_no_number_may_be_inserted(self):
        response = self._upload(anchor_pdf_page=1, page_number="")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.scan.page_edits.get().logical_page, "")

    def test_an_unplaceable_upload_is_refused(self):
        response = self._upload(page_number=99)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.scan.page_edits.exists())

    def test_a_file_that_is_not_an_image_is_refused(self):
        response = self.client.post(
            reverse("add_page_insert", kwargs={"pk": self.scan.pk}),
            data={"image": self.make_pdf(), "anchor_pdf_page": 1},
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.scan.page_edits.exists())

    def test_an_insert_can_be_removed(self):
        edit_id = json.loads(
            self._upload(anchor_pdf_page=1, page_number=2).content
        )["edit_id"]

        response = self.client.post(
            reverse("remove_page_insert", kwargs={"pk": self.scan.pk}),
            data=json.dumps({"edit_id": edit_id}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        edit = self.scan.page_edits.get()
        self.assertIsNotNone(edit.withdrawn_at)
        self.assertEqual(edit.withdrawn_by, self.user)
        self.assertEqual(page_edits.inserts_by_gap(self.scan), {})

    def test_a_removed_insert_keeps_its_file(self):
        edit_id = json.loads(
            self._upload(anchor_pdf_page=1, page_number=2).content
        )["edit_id"]
        name = PageEdit.objects.get(pk=edit_id).image.name

        self.client.post(
            reverse("remove_page_insert", kwargs={"pk": self.scan.pk}),
            data=json.dumps({"edit_id": edit_id}),
            content_type="application/json",
        )

        edit = PageEdit.objects.get(pk=edit_id)
        self.assertEqual(edit.image.name, name)
        self.assertTrue(edit.image.storage.exists(name))

    def test_removing_an_unknown_insert_is_a_404(self):
        response = self.client.post(
            reverse("remove_page_insert", kwargs={"pk": self.scan.pk}),
            data=json.dumps({"edit_id": 9999}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)


class TestReplaceAndRotateEndpoints(ScanningTestCase):
    """The two decisions the portal could not record at all."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.scan = ScanFactory(page_count=3, source_fingerprint="100:3")

    def test_a_blurry_page_is_replaced_in_one_row(self):
        response = self.client.post(
            reverse("replace_page", kwargs={"pk": self.scan.pk}),
            data={"image": self.make_image(), "pdf_page": 2},
        )

        self.assertEqual(response.status_code, 200)
        edit = self.scan.page_edits.get()
        self.assertEqual(edit.kind, PageEdit.Kind.REPLACE_PAGE)
        self.assertEqual(edit.pdf_page, 2)
        self.assertTrue(edit.image.name)

    def test_a_second_replacement_withdraws_the_first_row(self):
        for _ in range(2):
            self.client.post(
                reverse("replace_page", kwargs={"pk": self.scan.pk}),
                data={"image": self.make_image(), "pdf_page": 2},
            )

        rows = list(self.scan.page_edits.order_by("date_created"))
        self.assertEqual(len(rows), 2)
        self.assertIsNotNone(rows[0].withdrawn_at)
        self.assertIsNone(rows[1].withdrawn_at)
        # Both files stand: the audit shows every page a person sent,
        # and an overwritten field would leave the first with no row.
        self.assertNotEqual(rows[0].image.name, rows[1].image.name)
        for row in rows:
            self.assertTrue(row.image.storage.exists(row.image.name))
        self.assertEqual(list(page_edits.replacements_by_page(self.scan)), [2])

    def test_a_rotation_is_recorded(self):
        response = self.client.post(
            reverse("rotate_page", kwargs={"pk": self.scan.pk}),
            data=json.dumps({"pdf_page": 2, "degrees": "180"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.scan.page_edits.get().value, "180")

    def test_a_rotation_that_is_not_a_quarter_turn_is_refused(self):
        response = self.client.post(
            reverse("rotate_page", kwargs={"pk": self.scan.pk}),
            data=json.dumps({"pdf_page": 2, "degrees": "45"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.scan.page_edits.exists())


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestReplaceButton(ScanningTestCase):
    """The Replace button of review 1, and what it shows (issue #232)."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.scan = ScanFactory(
            page_count=3,
            source_fingerprint="100:3",
            status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
            ocr_results=[
                {
                    "pdf_page": n,
                    "detected": str(n),
                    "type": "single",
                    "score": 1.0,
                    "zone": "header",
                }
                for n in (1, 2, 3)
            ],
        )

    def _replace(self, pdf_page=2, upload=None):
        """Post one replacement.

        :param pdf_page: The page it stands for.
        :param upload: The file. A small PNG when None.
        :returns: The HTTP response.
        """
        return self.client.post(
            reverse("replace_page", kwargs={"pk": self.scan.pk}),
            data={"image": upload or self.make_image(), "pdf_page": pdf_page},
        )

    def _step_one(self):
        """Return the rendered step-1 page.

        :returns: The HTTP response.
        """
        return self.client.get(
            reverse("scan_process", kwargs={"pk": self.scan.pk}) + "?step=1"
        )

    def test_the_page_carries_the_replacement_for_the_viewer(self):
        edit_id = json.loads(self._replace().content)["edit_id"]

        context = self._step_one().context

        replaced = json.loads(context["replaced_pages_json"])
        self.assertEqual(list(replaced), ["2"])
        self.assertEqual(replaced["2"]["edit_id"], edit_id)
        self.assertEqual(replaced["2"]["kind"], "image")
        self.assertEqual(
            replaced["2"]["url"],
            reverse(
                "page_edit_file",
                kwargs={"pk": self.scan.pk, "edit_id": edit_id},
            ),
        )

    def test_the_sidebar_row_of_a_replaced_page_says_so(self):
        self._replace(pdf_page=2)

        response = self._step_one()

        rows = response.context["ocr_results"]
        self.assertEqual(
            [r["is_replaced"] for r in rows], [False, True, False]
        )
        self.assertContains(response, "REPL")

    def test_a_replacement_of_another_original_is_not_shown(self):
        self._replace()
        self.scan.page_edits.update(source_fingerprint="999:9")

        context = self._step_one().context

        self.assertEqual(json.loads(context["replaced_pages_json"]), {})

    def test_a_replacement_can_be_taken_back(self):
        self._replace(pdf_page=2)

        response = self.client.post(
            reverse("undo_replace_page", kwargs={"pk": self.scan.pk}),
            data=json.dumps({"pdf_page": 2}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        edit = self.scan.page_edits.get()
        self.assertIsNotNone(edit.withdrawn_at)
        self.assertEqual(edit.withdrawn_by, self.user)
        self.assertEqual(page_edits.replacements_by_page(self.scan), {})

    def test_a_page_can_be_replaced_again_after_an_undo(self):
        self._replace(pdf_page=2)
        self.client.post(
            reverse("undo_replace_page", kwargs={"pk": self.scan.pk}),
            data=json.dumps({"pdf_page": 2}),
            content_type="application/json",
        )

        self.assertEqual(self._replace(pdf_page=2).status_code, 200)

        self.assertEqual(self.scan.page_edits.count(), 2)
        self.assertEqual(list(page_edits.replacements_by_page(self.scan)), [2])

    def test_taking_back_a_replacement_that_is_not_there_is_a_no_op(self):
        """A second tab or a second click must not fail a page that is
        already back, as in ``undo_delete_page``."""
        response = self.client.post(
            reverse("undo_replace_page", kwargs={"pk": self.scan.pk}),
            data=json.dumps({"pdf_page": 2}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["status"], "ok")
        self.assertFalse(self.scan.page_edits.exists())

    def test_the_file_view_sends_the_reader_to_the_file(self):
        edit_id = json.loads(self._replace().content)["edit_id"]

        response = self.client.get(
            reverse(
                "page_edit_file",
                kwargs={"pk": self.scan.pk, "edit_id": edit_id},
            )
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(
            PageEdit.objects.get(pk=edit_id).image.name, response["Location"]
        )

    def test_the_file_view_serves_a_withdrawn_row(self):
        edit_id = json.loads(self._replace().content)["edit_id"]
        self.client.post(
            reverse("undo_replace_page", kwargs={"pk": self.scan.pk}),
            data=json.dumps({"pdf_page": 2}),
            content_type="application/json",
        )

        response = self.client.get(
            reverse(
                "page_edit_file",
                kwargs={"pk": self.scan.pk, "edit_id": edit_id},
            )
        )

        self.assertEqual(response.status_code, 302)

    def test_the_file_view_refuses_a_row_of_another_scan(self):
        other = ScanFactory(page_count=1)
        edit_id = json.loads(self._replace().content)["edit_id"]

        response = self.client.get(
            reverse(
                "page_edit_file",
                kwargs={"pk": other.pk, "edit_id": edit_id},
            )
        )

        self.assertEqual(response.status_code, 404)

    def test_the_file_view_needs_a_login(self):
        edit_id = json.loads(self._replace().content)["edit_id"]
        self.client.logout()

        response = self.client.get(
            reverse(
                "page_edit_file",
                kwargs={"pk": self.scan.pk, "edit_id": edit_id},
            )
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

    def test_the_approve_button_stands_although_edits_are_pending(self):
        self._replace()

        response = self._step_one()

        self.assertContains(
            response, "I reviewed this scan and it is complete"
        )
        self.assertNotContains(response, "Rebuild &amp; Validate")

    def test_the_banner_says_what_is_not_done_yet(self):
        self._replace()

        response = self._step_one()

        self.assertContains(response, "Your page changes are saved.")
        self.assertContains(
            response, "Approve this volume when the pages are complete"
        )
        self.assertNotContains(response, "#206")


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestPageUploadsTakeAPdf(ScanningTestCase):
    """A curator may send an image of a page or a PDF of it (#232)."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.scan = ScanFactory(page_count=3, source_fingerprint="100:3")

    @staticmethod
    def _pdf(pages=1, name="page.pdf"):
        """Return an uploaded PDF of the given length.

        :param pages: How many pages it holds.
        :param name: The name the browser sends.
        :returns: A SimpleUploadedFile holding a real PDF.
        """
        doc = fitz.open()
        for _ in range(pages):
            doc.new_page()
        data = doc.tobytes()
        doc.close()
        return SimpleUploadedFile(name, data, content_type="application/pdf")

    def _replace(self, upload, pdf_page=2):
        """Post one replacement.

        :param upload: The file to send.
        :param pdf_page: The page it stands for.
        :returns: The HTTP response.
        """
        return self.client.post(
            reverse("replace_page", kwargs={"pk": self.scan.pk}),
            data={"image": upload, "pdf_page": pdf_page},
        )

    def _insert(self, upload):
        """Post one insert into the gap after page 1.

        :param upload: The file to send.
        :returns: The HTTP response.
        """
        return self.client.post(
            reverse("add_page_insert", kwargs={"pk": self.scan.pk}),
            data={"image": upload, "anchor_pdf_page": 1, "page_number": "2"},
        )

    def test_a_pdf_replaces_a_page(self):
        response = self._replace(self._pdf())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["kind"], "pdf")
        self.assertTrue(self.scan.page_edits.get().image.name.endswith(".pdf"))

    def test_a_pdf_with_no_extension_is_stored_as_one(self):
        self._replace(self._pdf(name="scan"))

        self.assertTrue(self.scan.page_edits.get().image.name.endswith(".pdf"))

    def test_a_pdf_of_several_pages_is_refused_as_a_replacement(self):
        response = self._replace(self._pdf(pages=2))

        self.assertEqual(response.status_code, 400)
        self.assertIn("one page", json.loads(response.content)["error"])
        self.assertFalse(self.scan.page_edits.exists())

    def test_a_pdf_of_several_pages_is_accepted_as_an_insert(self):
        response = self._insert(self._pdf(pages=2))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["kind"], "pdf")

    def test_a_file_that_lies_about_being_a_pdf_is_refused(self):
        upload = SimpleUploadedFile(
            "page.pdf", b"not a pdf at all", content_type="application/pdf"
        )

        response = self._replace(upload)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.scan.page_edits.exists())

    def test_neither_an_image_nor_a_pdf_is_refused(self):
        upload = SimpleUploadedFile(
            "notes.txt", b"hello", content_type="text/plain"
        )

        response = self._replace(upload)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.scan.page_edits.exists())

    @override_settings(PAGE_UPLOAD_MAX_BYTES=2 * 1024 * 1024)
    def test_a_file_over_the_cap_is_refused(self):
        """The cap is the setting, and the refusal names its value in MB."""
        upload = SimpleUploadedFile(
            "page.png",
            b"x" * (2 * 1024 * 1024 + 1),
            content_type="image/png",
        )

        response = self._replace(upload)

        self.assertEqual(response.status_code, 400)
        self.assertIn("2 MB", json.loads(response.content)["error"])
        self.assertFalse(self.scan.page_edits.exists())

    def test_the_default_cap_takes_a_rescan_of_a_whole_gap(self):
        """A 90-page rescan of 138 MB was refused at 50 MB (so3d vol 361).

        A gap takes one insert (#256), so the file could not be split;
        the default is a sixth of the original upload cap instead.
        """
        self.assertEqual(
            settings.PAGE_UPLOAD_MAX_BYTES,
            settings.MAX_ORIGINAL_UPLOAD_SIZE // 6,
        )
        self.assertEqual(settings.PAGE_UPLOAD_MAX_BYTES, 512 * 1024 * 1024)
        self.assertGreater(settings.PAGE_UPLOAD_MAX_BYTES, 138 * 1024 * 1024)
        self.assertIn("512 MB", views_process.upload_too_large_message())

    def test_a_pdf_on_disk_is_counted_from_its_temporary_file(self):
        """A large upload lands in a temporary file; fitz opens that path.

        The bytes are not read into memory a second time, and the
        upload is left rewound for the storage write that follows.
        """
        with fitz.open() as doc:
            for _ in range(3):
                doc.new_page()
            pdf = doc.tobytes()
        upload = TemporaryUploadedFile(
            "leaf.pdf", "application/pdf", len(pdf), None
        )
        upload.write(pdf)
        upload.seek(0)
        with mock.patch.object(
            views_process.fitz, "open", wraps=fitz.open
        ) as opened:
            self.assertEqual(views_process._pdf_page_count(upload), 3)
        opened.assert_called_once_with(
            upload.temporary_file_path(), filetype="pdf"
        )
        self.assertEqual(upload.tell(), 0)
        upload.close()

    def test_an_image_keeps_its_own_extension(self):
        self._replace(self.make_image())

        self.assertTrue(self.scan.page_edits.get().image.name.endswith(".png"))

    def test_an_image_is_judged_by_its_bytes_not_its_name(self):
        """A JPEG named ``.png`` is stored as the JPEG it is."""
        jpeg = SimpleUploadedFile(
            "page.png",
            b"\xff\xd8\xff\xe0" + b"\x00" * 16,
            content_type="image/png",
        )

        response = self._replace(jpeg)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.scan.page_edits.get().image.name.endswith(".jpg"))

    def test_an_image_format_mupdf_cannot_open_is_refused(self):
        """An SVG passed on its content type alone and failed at the export."""
        svg = SimpleUploadedFile(
            "page.svg",
            b"<svg xmlns='http://www.w3.org/2000/svg'/>",
            content_type="image/svg+xml",
        )

        response = self._replace(svg)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            json.loads(response.content)["error"],
            views_process.UPLOAD_WRONG_TYPE_MESSAGE,
        )
        self.assertFalse(self.scan.page_edits.exists())

    def _stored_files(self):
        """Return the files under the scan's ``page_edits/`` prefix.

        :returns: The file names the storage holds there.
        """
        prefix = (
            f"{s3_sync.s3_processing_prefix(self.scan)}"
            f"{s3_sync.PAGE_EDITS_SUBDIR}"
        )
        return default_storage.listdir(prefix)[1]

    def test_a_replacement_that_loses_the_race_leaves_no_file(self):
        """Two replacements of one page at once: the second answers 409
        and takes its file back, so no object stays that no row names.

        The other request's row is committed before ours, and our
        withdrawal did not see it: that is the moment the partial
        unique key refuses our insert.
        """
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.REPLACE_PAGE,
            pdf_page=2,
            value="",
            image=self.make_image(),
        )
        with mock.patch.object(
            views_process.page_edits, "withdraw", return_value=0
        ):
            response = self._replace(self.make_image())

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            json.loads(response.content)["error"],
            views_process.UPLOAD_LOST_RACE_MESSAGE,
        )
        self.assertEqual(self.scan.page_edits.count(), 1)
        self.assertEqual(len(self._stored_files()), 1)

    def test_an_insert_that_loses_the_race_leaves_no_file(self):
        """Two inserts into one gap at once compute one ordinal."""
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.INSERT_PAGE,
            pdf_page=None,
            anchor_pdf_page=1,
            ordinal=0,
            logical_page="2",
            value="",
            image=self.make_image(),
        )
        with mock.patch.object(
            views_process.page_edits, "next_ordinal", return_value=0
        ):
            response = self._insert(self.make_image())

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.scan.page_edits.count(), 1)
        self.assertEqual(len(self._stored_files()), 1)


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestProjectInserts(TestCase):
    """What the viewer is given: the images in place, and the anchors."""

    def setUp(self):
        self.scan = ScanFactory(page_count=2)
        self.page_map = [
            {"type": "pdf_page", "pdf_index": 0, "logical_number": 1},
            {"type": "missing", "logical_number": 2},
            {"type": "pdf_page", "pdf_index": 1, "logical_number": 3},
        ]

    def _insert(self, anchor, **kwargs):
        """Create one insert row.

        :param anchor: The page the image follows.
        :param kwargs: Overrides for the factory.
        :returns: The row.
        """
        return PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.INSERT_PAGE,
            pdf_page=None,
            anchor_pdf_page=anchor,
            value="",
            **kwargs,
        )

    def test_every_placeholder_carries_its_anchor(self):
        from scanning import page_edits

        out = page_edits.project_inserts(self.scan, self.page_map)

        self.assertEqual(out[1]["anchor_pdf_page"], 1)

    def test_an_image_fills_the_placeholder_it_was_anchored_to(self):
        from scanning import page_edits

        edit = self._insert(1, logical_page="2")

        out = page_edits.project_inserts(self.scan, self.page_map)

        self.assertEqual(out[1]["type"], "inserted")
        self.assertEqual(out[1]["insert_edit_id"], edit.pk)
        self.assertEqual(out[1]["logical_number"], 2)
        self.assertEqual(len(out), 3)

    def test_an_image_before_page_one_comes_first(self):
        self._insert(0, logical_page="0")

        from scanning import page_edits

        out = page_edits.project_inserts(self.scan, self.page_map)

        self.assertEqual(out[0]["type"], "inserted")
        self.assertEqual(out[1]["type"], "pdf_page")

    def test_an_image_whose_placeholder_is_gone_is_still_shown(self):
        # A later OCR run read the number the placeholder stood for.
        # The uploaded page must not disappear with it.
        from scanning import page_edits

        self._insert(2, logical_page="4")

        out = page_edits.project_inserts(self.scan, self.page_map)

        self.assertEqual(out[-1]["type"], "inserted")
        self.assertEqual(out[-1]["logical_number"], "4")

    def test_an_image_past_the_last_page_is_unplaced(self):
        from scanning import page_edits

        self._insert(9, logical_page="9")

        out = page_edits.project_inserts(self.scan, self.page_map)

        self.assertEqual(out[-1]["type"], "inserted")
        self.assertTrue(out[-1]["unplaced"])

    def _moved_map(self):
        """A six-page scan whose page 4 moved to after page 2 (#261),
        and its page map in that order, with a placeholder between the
        two pages of the pair and one before the last page."""
        scan = ScanFactory(page_count=6, source_fingerprint="100:6")
        PageEditFactory(
            scan=scan,
            kind=PageEdit.Kind.MOVE_PAGE,
            pdf_page=4,
            anchor_pdf_page=2,
            value="",
            source_fingerprint="100:6",
        )
        page_map = [
            {"type": "pdf_page", "pdf_index": 0, "logical_number": 1},
            {"type": "pdf_page", "pdf_index": 1, "logical_number": 2},
            {"type": "pdf_page", "pdf_index": 3, "logical_number": 3},
            {"type": "missing", "logical_number": 4},
            {"type": "pdf_page", "pdf_index": 2, "logical_number": 5},
            {"type": "pdf_page", "pdf_index": 4, "logical_number": 6},
            {"type": "missing", "logical_number": 7},
            {"type": "pdf_page", "pdf_index": 5, "logical_number": 8},
        ]
        return scan, page_map

    def test_an_image_follows_the_slot_of_its_anchor_when_pages_moved(self):
        # The gap after page 4 is where page 4 was scanned, after page 3,
        # the place the apply gives it (``slot_order``); the viewer must
        # draw it there and not after the moved page.
        from scanning import page_edits

        scan, page_map = self._moved_map()
        after_four = PageEditFactory(
            scan=scan,
            kind=PageEdit.Kind.INSERT_PAGE,
            pdf_page=None,
            anchor_pdf_page=4,
            value="",
            source_fingerprint="100:6",
        )
        after_two = PageEditFactory(
            scan=scan,
            kind=PageEdit.Kind.INSERT_PAGE,
            pdf_page=None,
            anchor_pdf_page=2,
            value="",
            source_fingerprint="100:6",
        )

        out = page_edits.project_inserts(scan, page_map)

        shape = [
            (e["type"], e.get("pdf_index"), e.get("insert_edit_id"))
            for e in out
        ]
        self.assertEqual(
            shape,
            [
                ("pdf_page", 0, None),
                ("pdf_page", 1, None),
                ("pdf_page", 3, None),
                # The placeholder between the pair sits in gap 2 and
                # takes the image anchored there.
                ("inserted", None, after_two.pk),
                ("pdf_page", 2, None),
                # Where page 4 was: after page 3, before page 5.
                ("inserted", None, after_four.pk),
                ("pdf_page", 4, None),
                ("missing", None, None),
                ("pdf_page", 5, None),
            ],
        )
        self.assertFalse(any(e.get("unplaced") for e in out))
        self.assertEqual(out[7]["anchor_pdf_page"], 5)

    def test_a_placeholder_between_a_moved_pair_carries_the_anchor(self):
        from scanning import page_edits

        scan, page_map = self._moved_map()

        out = page_edits.project_inserts(scan, page_map)

        self.assertEqual(out[3]["type"], "missing")
        self.assertEqual(out[3]["anchor_pdf_page"], 2)
        self.assertEqual(out[6]["anchor_pdf_page"], 5)


class TestMigratePageInsertImagesCommand(ScanningTestCase):
    """The command that moves a migrated image off the pod's disk."""

    def _legacy_edit(self):
        """Create an insert row whose image is at the legacy key.

        :returns: The row.
        """
        from scanning.storage import LocalProcessingStorage

        name = LocalProcessingStorage().save(
            "page_inserts/old.png", self.make_image()
        )
        edit = PageEditFactory(
            scan=ScanFactory(),
            kind=PageEdit.Kind.INSERT_PAGE,
            pdf_page=None,
            anchor_pdf_page=1,
            value="",
        )
        PageEdit.objects.filter(pk=edit.pk).update(image=name)
        edit.refresh_from_db()
        return edit

    def test_the_image_moves_under_the_scans_prefix(self):
        from scanning import s3_sync

        edit = self._legacy_edit()

        call_command("migrate_page_insert_images")

        edit.refresh_from_db()
        self.assertTrue(
            edit.image.name.startswith(
                f"{s3_sync.s3_processing_prefix(edit.scan)}"
                f"{s3_sync.PAGE_EDITS_SUBDIR}"
            )
        )
        self.assertTrue(edit.image.storage.exists(edit.image.name))

    def test_a_dry_run_changes_nothing(self):
        edit = self._legacy_edit()

        call_command("migrate_page_insert_images", "--dry-run")

        edit.refresh_from_db()
        self.assertTrue(edit.image.name.startswith("page_inserts/"))

    def test_a_file_the_pod_lost_clears_the_field(self):
        edit = self._legacy_edit()
        edit.image.storage.delete(edit.image.name)

        call_command("migrate_page_insert_images")

        edit.refresh_from_db()
        self.assertEqual(edit.image.name, "")


class TestExportPdfAppliesTheEdits(ScanningTestCase):
    """``views_api.export_pdf`` reads the rows, in the original's space."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.scan = ScanFactory(page_count=4)
        output_dir = pathlib.Path(self.scan.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        original = output_dir / pathlib.Path(self.scan.original_pdf.name).name
        write_image_volume(original, pages=4)

    def _export(self):
        """Ask for the corrected PDF and open it.

        :returns: The exported document's page count.
        :rtype: int
        """
        response = self.client.get(
            reverse("export_pdf", kwargs={"pk": self.scan.pk})
        )
        self.assertEqual(response.status_code, 200)
        data = b"".join(response.streaming_content)
        with fitz.open(stream=data, filetype="pdf") as doc:
            return doc.page_count

    def test_a_deleted_page_is_dropped(self):
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.DELETE_PAGE,
            pdf_page=2,
            value="",
        )

        self.assertEqual(self._export(), 3)

    def test_an_inserted_image_is_added(self):
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.INSERT_PAGE,
            pdf_page=None,
            anchor_pdf_page=1,
            value="",
            image=self.make_image(),
        )

        self.assertEqual(self._export(), 5)

    def test_an_applied_edit_still_stands(self):
        # An applied deletion is still a deletion (#224): the next
        # build must see it, or the second final PDF would restore the
        # page in silence. Only a withdrawal takes a decision back.
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.DELETE_PAGE,
            pdf_page=2,
            value="",
            applied_at=timezone.now(),
        )

        self.assertEqual(self._export(), 3)

    def test_a_withdrawn_edit_is_not_applied(self):
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.DELETE_PAGE,
            pdf_page=2,
            value="",
            applied_at=timezone.now(),
            withdrawn_at=timezone.now(),
        )

        self.assertEqual(self._export(), 4)

    def test_a_delete_and_an_insert_do_not_move_each_other(self):
        # The anchor names a page of the original, so the position it
        # points at moves by every page removed before it.
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.DELETE_PAGE,
            pdf_page=1,
            value="",
        )
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.INSERT_PAGE,
            pdf_page=None,
            anchor_pdf_page=3,
            value="",
            image=self.make_image(),
        )

        self.assertEqual(self._export(), 4)


class TestInsertMigrationAnchor(TestCase):
    """The #214 migration's resolution of an insert's anchor."""

    def _anchor_for(self, page_map, logical_number):
        """Call the migration's helper.

        :param page_map: A stored page map.
        :param logical_number: The printed number of the insert.
        :returns: The anchor it resolves.
        """
        from importlib import import_module

        migration = import_module(
            "scanning.migrations.0015_page_edits_from_inserts_and_deletions"
        )
        return migration._anchor_for(page_map, logical_number)

    def test_the_placeholder_is_the_exact_answer(self):
        page_map = [
            {"type": "pdf_page", "pdf_index": 0, "logical_number": 1},
            {"type": "missing", "logical_number": 2},
            {"type": "pdf_page", "pdf_index": 1, "logical_number": 3},
        ]

        self.assertEqual(self._anchor_for(page_map, 2), 1)

    def test_a_gone_placeholder_falls_back_to_the_neighbour(self):
        page_map = [
            {"type": "pdf_page", "pdf_index": 0, "logical_number": 1},
            {"type": "pdf_page", "pdf_index": 1, "logical_number": 3},
        ]

        self.assertEqual(self._anchor_for(page_map, 2), 1)

    def test_an_insert_before_the_first_page_anchors_at_zero(self):
        page_map = [
            {"type": "pdf_page", "pdf_index": 0, "logical_number": 5},
            {"type": "missing", "logical_number": 4},
        ]

        self.assertEqual(self._anchor_for(page_map, 4), 1)

    def test_a_volume_with_no_page_map_cannot_place_it(self):
        self.assertIsNone(self._anchor_for([], 2))


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestStructuralEditsAgainstAnotherOriginal(TestCase):
    """A delete or an insert from another original must not be acted on.

    The page numbers already refuse to be placed on a volume whose
    fingerprint moved. The structural kinds are the ones that would do
    real damage: a delete drops a page of a document the curator never
    saw.
    """

    def setUp(self):
        self.scan = ScanFactory(page_count=4, source_fingerprint="200:4")

    def test_a_stale_deletion_is_not_reported_as_a_deletion(self):
        from scanning import page_edits

        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.DELETE_PAGE,
            pdf_page=2,
            value="",
            source_fingerprint="100:4",
        )

        self.assertEqual(page_edits.deleted_pages(self.scan), set())

    def test_a_stale_insert_is_not_handed_to_the_apply(self):
        from scanning import page_edits

        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.INSERT_PAGE,
            pdf_page=None,
            anchor_pdf_page=1,
            value="",
            source_fingerprint="100:4",
        )

        self.assertEqual(page_edits.inserts_by_gap(self.scan), {})

    def test_a_stale_structural_edit_is_reported(self):
        from scanning import services

        Scan.objects.filter(pk=self.scan.pk).update(
            start_page=1,
            end_page=4,
            ocr_results=[
                {
                    "pdf_page": n,
                    "detected": str(n),
                    "type": "single",
                    "zone": "dots-header",
                }
                for n in range(1, 5)
            ],
        )
        self.scan.refresh_from_db()
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.DELETE_PAGE,
            pdf_page=2,
            value="",
            source_fingerprint="100:4",
        )

        services.recalculate_issues(self.scan)

        self.assertTrue(
            self.scan.issues.filter(
                check_name=CheckName.STALE_PAGE_EDIT, page_number=2
            ).exists()
        )


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestAnInsertTheMapCannotPlace(TestCase):
    """An image the page map cannot place is shown, never hidden.

    Its Remove button is the only way to take it back, so hiding the
    image strands the row: nothing else in the portal can reach it.
    """

    def setUp(self):
        self.scan = ScanFactory(page_count=2)
        self.page_map = [
            {"type": "pdf_page", "pdf_index": 0, "logical_number": 1},
            {"type": "pdf_page", "pdf_index": 1, "logical_number": 3},
        ]

    def test_an_anchor_beyond_the_page_map_is_still_shown(self):
        from scanning import page_edits

        edit = PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.INSERT_PAGE,
            pdf_page=None,
            anchor_pdf_page=7,
            value="",
        )

        out = page_edits.project_inserts(self.scan, self.page_map)

        shown = [e for e in out if e.get("insert_edit_id") == edit.pk]
        self.assertEqual(len(shown), 1)

    def test_an_insert_on_a_volume_with_no_page_map_is_still_shown(self):
        from scanning import page_edits

        edit = PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.INSERT_PAGE,
            pdf_page=None,
            anchor_pdf_page=1,
            value="",
        )

        out = page_edits.project_inserts(self.scan, [])

        self.assertEqual([e["insert_edit_id"] for e in out], [edit.pk])

    def test_an_insert_on_a_page_the_map_lost_is_flagged(self):
        """The anchor page is not in the map, so the walk has no slot.

        The apply's plan walks every original page and places the
        image after the anchor. The map is the one thing that loses a
        page, so the viewer says so instead of drawing the image at a
        position the plan does not share.
        """
        from scanning import page_edits

        scan = ScanFactory(page_count=3)
        page_map = [
            {"type": "pdf_page", "pdf_index": 0, "logical_number": 1},
            {"type": "pdf_page", "pdf_index": 2, "logical_number": 3},
        ]
        edit = PageEditFactory(
            scan=scan,
            kind=PageEdit.Kind.INSERT_PAGE,
            pdf_page=None,
            anchor_pdf_page=2,
            value="",
        )

        out = page_edits.project_inserts(scan, page_map)

        self.assertEqual(out[-1]["insert_edit_id"], edit.pk)
        self.assertTrue(out[-1]["unplaced"])

    def test_a_placeholder_carries_the_last_gap_the_map_holds(self):
        """A page the map lost moves no anchor.

        The stamp is the address an upload comes back under, so it
        names a page the volume shows. Page 2 is not in the map, and
        the placeholder between pages 1 and 3 still says page 1.
        """
        from scanning import page_edits

        scan = ScanFactory(page_count=3)
        page_map = [
            {"type": "pdf_page", "pdf_index": 0, "logical_number": 1},
            {"type": "missing", "logical_number": 2},
            {"type": "pdf_page", "pdf_index": 2, "logical_number": 3},
        ]

        out = page_edits.project_inserts(scan, page_map)

        self.assertEqual(out[1]["anchor_pdf_page"], 1)


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestPendingEditFlags(TestCase):
    """The two flags the step-1 bar reads come from one read of one set."""

    def setUp(self):
        self.scan = ScanFactory(page_count=4, source_fingerprint="200:4")

    def test_no_edits_means_no_flags(self):
        from scanning import page_edits

        self.assertEqual(
            page_edits.pending_edit_flags(self.scan),
            {"has_pending_changes": False, "has_pending_inserts": False},
        )

    def test_a_deletion_is_a_change_and_costs_no_run(self):
        from scanning import page_edits

        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.DELETE_PAGE,
            pdf_page=2,
            value="",
        )

        self.assertEqual(
            page_edits.pending_edit_flags(self.scan),
            {"has_pending_changes": True, "has_pending_inserts": False},
        )

    def test_an_image_costs_a_run(self):
        from scanning import page_edits

        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.REPLACE_PAGE,
            pdf_page=2,
            value="",
        )

        self.assertEqual(
            page_edits.pending_edit_flags(self.scan),
            {"has_pending_changes": True, "has_pending_inserts": True},
        )

    def test_a_stale_insert_raises_neither_flag(self):
        # The two used to be read separately, and disagreed here: the
        # insert flag counted a row the change flag refused.
        from scanning import page_edits

        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.INSERT_PAGE,
            pdf_page=None,
            anchor_pdf_page=1,
            value="",
            source_fingerprint="100:4",
        )

        self.assertEqual(
            page_edits.pending_edit_flags(self.scan),
            {"has_pending_changes": False, "has_pending_inserts": False},
        )

    def test_a_page_number_is_not_a_pending_change(self):
        from scanning import page_edits

        PageEditFactory(scan=self.scan, pdf_page=2, value="7")

        self.assertFalse(
            page_edits.pending_edit_flags(self.scan)["has_pending_changes"]
        )


class TestSortedWindow(TestCase):
    """``page_edits.sorted_window``, the card's one rule (#261, #395)."""

    @staticmethod
    def _seq(numbers, first_page=1):
        return [(first_page + i, n) for i, n in enumerate(numbers)]

    def _window(self, numbers, first_page=1):
        """The answer of the one backward step in ``numbers``."""
        seq = self._seq(numbers, first_page)
        steps = [i for i in range(1, len(seq)) if seq[i][1] < seq[i - 1][1]]
        self.assertEqual(len(steps), 1, "one backward step per case")
        return page_edits.sorted_window(seq, steps[0])

    def _order(self, numbers, window, first_page=1):
        """The printed numbers the window's rows produce, read through
        ``slot_order`` as the plan and the sidebar read them."""
        seq = dict(self._seq(numbers, first_page))
        moves = {
            m["pdf_page"]: m["anchor_pdf_page"]
            for m in sorted(
                window["moves"],
                key=lambda m: (
                    m["anchor_pdf_page"],
                    m["ordinal"],
                    m["pdf_page"],
                ),
            )
        }
        last = first_page + len(numbers) - 1
        pages = [
            n
            for kind, n in page_edits.slot_order(last, moves)
            if kind == "page"
        ]
        return [seq[p] for p in pages if p in seq]

    def test_a_transposed_pair_is_the_swap(self):
        window = self._window([878, 880, 879, 881], first_page=883)

        self.assertEqual(window["pdf_pages"], [884, 885])
        self.assertEqual(window["order"], [885, 884])
        self.assertTrue(window["swap"])
        self.assertEqual(
            window["moves"],
            [{"pdf_page": 885, "anchor_pdf_page": 883, "ordinal": 0}],
        )

    def test_a_pair_at_the_start_goes_before_page_1(self):
        window = self._window([2, 1, 3])

        self.assertEqual(
            window["moves"],
            [{"pdf_page": 2, "anchor_pdf_page": 0, "ordinal": 0}],
        )

    def test_a_page_pulled_early_goes_after_the_run(self):
        # Scan 3409 of the issue.
        numbers = [785, 788, 786, 787, 789, 790]
        window = self._window(numbers, first_page=785)

        self.assertEqual(window["pdf_pages"], [786, 787, 788])
        self.assertFalse(window["swap"])
        self.assertEqual(
            window["moves"],
            [{"pdf_page": 786, "anchor_pdf_page": 788, "ordinal": 0}],
        )
        self.assertEqual(
            self._order(numbers, window, 785), [785, 786, 787, 788, 789, 790]
        )

    def test_a_page_pulled_late_goes_after_its_predecessor(self):
        numbers = [785, 787, 788, 786, 789]
        window = self._window(numbers, first_page=785)

        self.assertEqual(
            window["moves"],
            [{"pdf_page": 788, "anchor_pdf_page": 785, "ordinal": 0}],
        )
        self.assertEqual(
            self._order(numbers, window, 785), [785, 786, 787, 788, 789]
        )

    def test_two_blocks_the_wrong_way_round_move_the_smaller_one(self):
        # 184 and 185 scanned after 186..189: two rows, both landing
        # after the page printing 183, in ordinal order.
        numbers = [183, 186, 187, 188, 189, 184, 185, 190, 191]
        window = self._window(numbers, first_page=4)

        self.assertEqual(window["pdf_pages"], [5, 6, 7, 8, 9, 10])
        self.assertEqual(window["order"], [9, 10, 5, 6, 7, 8])
        self.assertEqual(
            window["moves"],
            [
                {"pdf_page": 9, "anchor_pdf_page": 4, "ordinal": 0},
                {"pdf_page": 10, "anchor_pdf_page": 4, "ordinal": 1},
            ],
        )
        self.assertEqual(
            self._order(numbers, window, 4), list(range(183, 192))
        )

    def test_a_block_pulled_early_lands_after_the_run_it_skipped(self):
        numbers = [1, 2, 7, 8, 3, 4, 5, 6, 9]
        window = self._window(numbers)

        self.assertEqual(
            window["moves"],
            [
                {"pdf_page": 3, "anchor_pdf_page": 8, "ordinal": 0},
                {"pdf_page": 4, "anchor_pdf_page": 8, "ordinal": 1},
            ],
        )
        self.assertEqual(self._order(numbers, window), list(range(1, 10)))

    def test_a_span_in_reverse_needs_the_ordinals(self):
        # Four pages scanned backwards: no choice of anchors puts them
        # right by page order, so the rows carry ordinals.
        numbers = [1, 5, 4, 3, 2, 6]
        seq = self._seq(numbers)
        for step in (2, 3, 4):
            window = page_edits.sorted_window(seq, step)
            self.assertEqual(window["pdf_pages"], [2, 3, 4, 5])
            self.assertEqual(
                window["moves"],
                [
                    {"pdf_page": 5, "anchor_pdf_page": 1, "ordinal": 0},
                    {"pdf_page": 4, "anchor_pdf_page": 1, "ordinal": 1},
                    {"pdf_page": 3, "anchor_pdf_page": 1, "ordinal": 2},
                ],
            )
            self.assertEqual(self._order(numbers, window), [1, 2, 3, 4, 5, 6])

    def test_the_window_reaches_over_to_its_boundary_number(self):
        # 4, 3, 2 after 1: the pair 4, 3 is complete but 2 sits on the
        # far side, so the window takes it in.
        numbers = [1, 4, 3, 2, 5]
        seq = self._seq(numbers)
        for step in (2, 3):
            window = page_edits.sorted_window(seq, step)
            self.assertEqual(window["pdf_pages"], [2, 3, 4])
            self.assertEqual(self._order(numbers, window), [1, 2, 3, 4, 5])

    def test_a_shuffle_of_many_pages_sorts_whole(self):
        numbers = [10, 14, 12, 16, 11, 15, 13, 17]
        seq = self._seq(numbers)
        steps = [i for i in range(1, len(seq)) if seq[i][1] < seq[i - 1][1]]
        for step in steps:
            window = page_edits.sorted_window(seq, step)
            self.assertEqual(window["pdf_pages"], [2, 3, 4, 5, 6, 7])
            self.assertEqual(window["order"], [5, 3, 7, 2, 6, 4])
            self.assertEqual(self._order(numbers, window), list(range(10, 18)))

    def test_a_run_that_stops_short_is_not_answered(self):
        # 788 pulled early, but 787 is nowhere: a hole in the window.
        self.assertIsNone(self._window([785, 788, 786, 790, 791]))

    def test_a_window_beside_a_hole_is_not_answered(self):
        # 788 early and 785 missing: the number before the window does
        # not continue it, so the reading is not trusted.
        self.assertIsNone(self._window([784, 788, 786, 787, 789]))
        # A transposed pair between two holes, the same.
        self.assertIsNone(self._window([183, 187, 186, 189]))

    def test_a_misread_is_not_answered(self):
        self.assertIsNone(self._window([1, 2, 3, 6, 4, 7, 8]))
        self.assertIsNone(self._window([1, 2, 50, 3, 5, 6]))
        self.assertIsNone(self._window([87, 279, 89, 90]))

    def test_a_duplicate_inside_the_window_is_not_answered(self):
        self.assertIsNone(self._window([1, 3, 2, 3, 4]))

    def test_a_span_that_crosses_another_page_is_not_answered(self):
        # A lettered page between 4 and 3 breaks the neighbours.
        seq = [(1, 1), (2, 2), (3, 4), (5, 3), (6, 5)]
        self.assertIsNone(page_edits.sorted_window(seq, 3))
        # The same with a run: 5 early, an unnumbered page in the run.
        seq = [(1, 1), (2, 2), (3, 5), (4, 3), (6, 4), (7, 6)]
        self.assertIsNone(page_edits.sorted_window(seq, 3))

    def test_an_index_off_the_run_is_not_answered(self):
        self.assertIsNone(page_edits.sorted_window([(1, 2), (2, 1)], 0))
        self.assertIsNone(page_edits.sorted_window([(1, 2), (2, 1)], 2))

    def test_the_label_names_the_shape(self):
        self.assertEqual(
            page_edits.move_label(self._window([1, 2, 4, 3, 5])),
            "Swap PDF pages 3 and 4",
        )
        self.assertEqual(
            page_edits.move_label(self._window([1, 2, 5, 3, 4, 6])),
            "Move PDF page 3 to after PDF page 5",
        )
        # 1 pulled late: one row, the page after the run it belongs before.
        self.assertEqual(
            page_edits.move_label(self._window([3, 1, 2, 4])),
            "Move PDF page 1 to after PDF page 3",
        )
        # 3 pulled early to the front: one row, before page 1.
        self.assertEqual(
            page_edits.move_label(self._window([2, 3, 1, 4])),
            "Move PDF page 3 to before PDF page 1",
        )
        window = page_edits.sorted_window(self._seq([1, 5, 4, 3, 2, 6]), 2)
        self.assertEqual(
            page_edits.move_label(window),
            "Reorder PDF pages 2 to 5 by printed number",
        )
        self.assertEqual(
            page_edits.move_title(window),
            "Put PDF pages 5, 4, 3, 2 in that order",
        )


class TestSlotOrder(TestCase):
    """The one rule of where a moved page goes (#261)."""

    def _results(self, detected):
        return [
            {"pdf_page": i, "detected": d, "type": "single"}
            for i, d in enumerate(detected, 1)
        ]

    def test_several_pages_on_one_anchor_land_in_the_order_given(self):
        # A span in reverse (#395): the dict's order is the landing
        # order, ordinal then page as ``moves_by_page`` yields it, not
        # page order.
        events = list(page_edits.slot_order(6, {5: 1, 4: 1, 3: 1}))
        self.assertEqual(
            [n for kind, n in events if kind == "page"], [1, 5, 4, 3, 2, 6]
        )

    def test_a_transposed_pair_reads_in_order_once_moved(self):
        results = self._results(["1", "2", "4", "3", "5", "6"])

        ordered = page_edits.order_by_moves(results, {4: 2})

        self.assertEqual([r["detected"] for r in ordered], list("123456"))
        self.assertEqual([r["pdf_page"] for r in ordered], [1, 2, 4, 3, 5, 6])
        # The same dicts, so a later write on one is seen by both lists.
        self.assertIs(ordered[2], results[3])
        # The cache is not touched.
        self.assertEqual([r["pdf_page"] for r in results], [1, 2, 3, 4, 5, 6])

    def test_no_move_is_a_copy_in_the_same_order(self):
        results = self._results(["1", "2"])
        ordered = page_edits.order_by_moves(results, {})
        self.assertEqual(ordered, results)
        self.assertIsNot(ordered, results)

    def test_the_events_of_the_slots(self):
        events = list(page_edits.slot_order(4, {3: 0, 1: 4}, anchors=[9]))
        self.assertEqual(
            events,
            [
                ("page", 3),
                ("gap", 0),
                ("gap", 1),
                ("page", 2),
                ("gap", 2),
                ("gap", 3),
                ("page", 4),
                ("page", 1),
                ("gap", 4),
                ("gap", 9),
            ],
        )

    def test_an_entry_no_slot_names_keeps_its_place_at_the_end(self):
        results = self._results(["1", "2", "3"])
        results.append({"pdf_page": 9, "detected": "9", "type": "single"})
        ordered = page_edits.order_by_moves(results, {2: 0})
        self.assertEqual([r["pdf_page"] for r in ordered], [2, 1, 3, 9])


class TestMovePageEndpoints(ScanningTestCase):
    """``move_page`` and its undo (#261), over the rows and the page."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.scan = self._scan(["1", "2", "4", "3", "5", "6"])

    def _scan(self, detected, start_page=None, end_page=None):
        """A reviewed scan whose pages read as ``detected``.

        The printed range defaults to one number per page from 1, so a
        volume that starts higher passes its own range, or the readings
        are out of range and corrected before any card is built.
        """
        return ScanFactory(
            page_count=len(detected),
            source_fingerprint=f"100:{len(detected)}",
            status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
            start_page=start_page or 1,
            end_page=end_page or len(detected),
            ocr_results=[
                {
                    "pdf_page": i,
                    "detected": d,
                    "type": "single",
                    "score": 1.0,
                    "zone": "dots-header",
                }
                for i, d in enumerate(detected, 1)
            ],
        )

    def _post(self, name, **body):
        return self.client.post(
            reverse(name, kwargs={"pk": self.scan.pk}),
            data=json.dumps(body),
            content_type="application/json",
        )

    def _step_one(self, scan=None):
        scan = scan or self.scan
        from scanning import services

        services.recalculate_issues(scan)
        return self.client.get(
            reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=1"
        )

    def _map_order(self):
        self.scan.refresh_from_db()
        return [
            e["pdf_index"] + 1
            for e in self.scan.page_map
            if e.get("type") == "pdf_page"
        ]

    def test_a_move_becomes_one_row_and_reorders_the_page_map(self):
        response = self._post("move_page", pdf_page=4, anchor_pdf_page=2)

        self.assertEqual(response.status_code, 200)
        answer = response.json()
        self.assertEqual(answer["status"], "ok")
        self.assertEqual(
            (answer["pdf_page"], answer["anchor_pdf_page"]), (4, 2)
        )
        self.assertEqual(len(answer["moves"]), 1)
        edit = self.scan.page_edits.get()
        self.assertEqual(edit.kind, PageEdit.Kind.MOVE_PAGE)
        self.assertEqual((edit.pdf_page, edit.anchor_pdf_page), (4, 2))
        self.assertEqual(edit.author, self.user)
        self.assertEqual(edit.source_fingerprint, "100:6")
        self.assertEqual(self._map_order(), [1, 2, 4, 3, 5, 6])
        self.scan.refresh_from_db()
        self.assertEqual(
            [r["pdf_page"] for r in self.scan.ocr_results], [1, 2, 3, 4, 5, 6]
        )

    def test_a_second_move_of_the_page_refreshes_the_row(self):
        self._post("move_page", pdf_page=4, anchor_pdf_page=2)
        self._post("move_page", pdf_page=4, anchor_pdf_page=0)

        edit = self.scan.page_edits.get()
        self.assertEqual(edit.anchor_pdf_page, 0)
        self.assertEqual(self._map_order(), [4, 1, 2, 3, 5, 6])

    def test_the_addresses_are_checked_before_anything_is_written(self):
        for body, status in (
            ({"pdf_page": 4, "anchor_pdf_page": 4}, 409),
            ({"pdf_page": 7, "anchor_pdf_page": 2}, 404),
            ({"pdf_page": 4, "anchor_pdf_page": 7}, 404),
            ({"pdf_page": 4}, 404),
            ({"anchor_pdf_page": 2}, 404),
            ({"pdf_page": "x", "anchor_pdf_page": 2}, 404),
        ):
            with self.subTest(body=body):
                self.assertEqual(
                    self._post("move_page", **body).status_code, status
                )
        self.assertEqual(self.scan.page_edits.count(), 0)
        self.assertEqual(
            self._post("move_page", pdf_page=4, anchor_pdf_page=4).json()[
                "error"
            ],
            views_process.MOVE_ONTO_ITSELF_MESSAGE,
        )

    def test_an_undo_withdraws_the_row_and_restores_the_order(self):
        self._post("move_page", pdf_page=4, anchor_pdf_page=2)

        response = self._post("undo_move_page", pdf_page=4)

        self.assertEqual(response.status_code, 200)
        edit = self.scan.page_edits.get()
        self.assertIsNotNone(edit.withdrawn_at)
        self.assertEqual(edit.withdrawn_by, self.user)
        self.assertEqual(self._map_order(), [1, 2, 3, 4, 5, 6])
        # A second undo is a no-op.
        self.assertEqual(
            self._post("undo_move_page", pdf_page=4).status_code, 200
        )

    def test_the_move_answers_the_backward_card_on_the_next_recompute(self):
        from scanning import services

        services.recalculate_issues(self.scan)
        self.assertEqual(
            self.scan.issues.filter(
                check_name=CheckName.BACKWARD_PAGE
            ).count(),
            1,
        )

        self._post("move_page", pdf_page=4, anchor_pdf_page=2)
        services.recalculate_issues(self.scan)

        self.assertFalse(
            self.scan.issues.filter(
                check_name=CheckName.BACKWARD_PAGE
            ).exists()
        )
        self.assertFalse(
            self.scan.issues.filter(check_name=CheckName.MISSING_PAGE).exists()
        )

    def test_the_card_of_a_transposed_pair_offers_the_swap(self):
        html = self._step_one().content.decode()

        self.assertIn("movePage(this)", html)
        self.assertIn(
            'data-moves="[{&quot;pdf_page&quot;: 4, &quot;anchor_pdf_page&quot;: 2, '
            '&quot;ordinal&quot;: 0}]"',
            html,
        )
        self.assertIn("Swap PDF pages 3 and 4", html)
        self.assertIn('title="Put PDF pages 4, 3 in that order"', html)
        # The sidebar still shows the scanned order, with the divider.
        self.assertIn("ORDER", html)
        self.assertNotIn("page-moved-badge", html)

    def test_a_page_pulled_early_offers_a_move_past_the_run(self):
        # The case of scan 3409 (#395): 5 scanned two slots early. The
        # card on 3 moves the page printing 5 to after the page
        # printing 4.
        scan = self._scan(["1", "2", "5", "3", "4", "6"])
        html = self._step_one(scan).content.decode()

        self.assertIn("goes backward", html)
        self.assertIn("movePage(this)", html)
        self.assertIn(
            "&quot;pdf_page&quot;: 3, &quot;anchor_pdf_page&quot;: 5", html
        )
        self.assertIn("Move PDF page 3 to after PDF page 5", html)
        self.assertNotIn("Swap PDF pages", html)

    def test_a_page_pulled_late_offers_a_move_back_to_its_place(self):
        # The mirror shape: 3 scanned two slots late goes after the
        # page printing 2.
        scan = self._scan(["1", "2", "4", "5", "3", "6"])
        html = self._step_one(scan).content.decode()

        self.assertIn("movePage(this)", html)
        self.assertIn(
            "&quot;pdf_page&quot;: 5, &quot;anchor_pdf_page&quot;: 2", html
        )
        self.assertIn("Move PDF page 5 to after PDF page 2", html)

    def test_two_blocks_the_wrong_way_round_offer_one_reorder(self):
        # The case of the issue's second comment: 184 and 185 scanned
        # after 186..189. One button, two rows.
        scan = self._scan(
            ["183", "186", "187", "188", "189", "184", "185", "190", "191"],
            start_page=183,
            end_page=191,
        )
        html = self._step_one(scan).content.decode()

        self.assertEqual(html.count("movePage(this)"), 1)
        self.assertIn("Reorder PDF pages 2 to 7 by printed number", html)
        self.assertIn(
            'title="Put PDF pages 6, 7, 2, 3, 4, 5 in that order"', html
        )
        self.assertIn(
            "&quot;pdf_page&quot;: 6, &quot;anchor_pdf_page&quot;: 1, "
            "&quot;ordinal&quot;: 0",
            html,
        )
        self.assertIn(
            "&quot;pdf_page&quot;: 7, &quot;anchor_pdf_page&quot;: 1, "
            "&quot;ordinal&quot;: 1",
            html,
        )

    def test_a_span_in_reverse_offers_the_reorder_on_each_card(self):
        scan = self._scan(["1", "5", "4", "3", "2", "6"])
        html = self._step_one(scan).content.decode()

        self.assertEqual(html.count('data-check="backward_page"'), 3)
        self.assertEqual(
            html.count("Reorder PDF pages 2 to 5 by printed number"), 3
        )

    def test_a_reorder_writes_every_row_and_lists_the_corrected_order(self):
        self.scan = self._scan(
            ["183", "186", "187", "188", "189", "184", "185", "190", "191"],
            start_page=183,
            end_page=191,
        )
        response = self._post(
            "move_page",
            moves=[
                {"pdf_page": 6, "anchor_pdf_page": 1, "ordinal": 0},
                {"pdf_page": 7, "anchor_pdf_page": 1, "ordinal": 1},
            ],
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["moves"]), 2)
        rows = {
            e.pdf_page: (e.anchor_pdf_page, e.ordinal)
            for e in page_edits.current_edits(
                self.scan, PageEdit.Kind.MOVE_PAGE
            )
        }
        self.assertEqual(rows, {6: (1, 0), 7: (1, 1)})
        self.assertEqual(self._map_order(), [1, 6, 7, 2, 3, 4, 5, 8, 9])

        html = self._step_one().content.decode()
        self.assertNotIn("movePage(this)", html)
        self.assertNotIn("goes backward", html)
        self.assertEqual(html.count("page-moved-badge"), 2)

    def test_a_reversed_span_reads_in_order_once_reordered(self):
        self.scan = self._scan(["1", "5", "4", "3", "2", "6"])
        response = self._post(
            "move_page",
            moves=[
                {"pdf_page": 5, "anchor_pdf_page": 1, "ordinal": 0},
                {"pdf_page": 4, "anchor_pdf_page": 1, "ordinal": 1},
                {"pdf_page": 3, "anchor_pdf_page": 1, "ordinal": 2},
            ],
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._map_order(), [1, 5, 4, 3, 2, 6])
        html = self._step_one().content.decode()
        self.assertNotIn("goes backward", html)
        self.assertFalse(
            self.scan.issues.filter(check_name=CheckName.MISSING_PAGE).exists()
        )

    def test_a_reorder_with_a_bad_address_writes_nothing(self):
        response = self._post(
            "move_page",
            moves=[
                {"pdf_page": 4, "anchor_pdf_page": 2},
                {"pdf_page": 99, "anchor_pdf_page": 2},
            ],
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(
            page_edits.current_edits(self.scan, PageEdit.Kind.MOVE_PAGE)
        )

    def test_a_reorder_that_names_a_page_twice_is_refused(self):
        response = self._post(
            "move_page",
            moves=[
                {"pdf_page": 4, "anchor_pdf_page": 2},
                {"pdf_page": 4, "anchor_pdf_page": 1},
            ],
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(
            page_edits.current_edits(self.scan, PageEdit.Kind.MOVE_PAGE)
        )

    def test_an_empty_reorder_is_refused(self):
        self.assertEqual(self._post("move_page", moves=[]).status_code, 400)
        self.assertEqual(self._post("move_page", moves=["4"]).status_code, 400)
        self.assertEqual(
            self._post(
                "move_page",
                moves=[{"pdf_page": 4, "anchor_pdf_page": 2, "ordinal": -1}],
            ).status_code,
            400,
        )

    def test_moves_by_page_yields_the_landing_order(self):
        self.scan = self._scan(["1", "5", "4", "3", "2", "6"])
        for pdf_page, ordinal in ((3, 2), (5, 0), (4, 1)):
            PageEditFactory(
                scan=self.scan,
                kind=PageEdit.Kind.MOVE_PAGE,
                pdf_page=pdf_page,
                anchor_pdf_page=1,
                ordinal=ordinal,
                source_fingerprint=self.scan.source_fingerprint,
            )

        self.assertEqual(list(page_edits.moves_by_page(self.scan)), [5, 4, 3])

    def test_a_misread_gets_no_button(self):
        # 6 read on page 4 of a volume whose numbers are all in range,
        # so nothing corrects it: the step back to 4 fits no shape, and
        # the card offers nothing.
        scan = self._scan(["1", "2", "3", "6", "4", "7", "8"])
        html = self._step_one(scan).content.decode()

        self.assertIn("goes backward", html)
        self.assertNotIn("movePage(this)", html)

    def test_a_displaced_page_the_volume_prints_twice_gets_no_button(self):
        # 5 on page 3 fits the pulled-early shape, but the volume prints
        # 5 again later, so the card cannot tell which page is out of
        # place.
        scan = self._scan(["1", "2", "5", "3", "4", "5", "6"])
        html = self._step_one(scan).content.decode()

        self.assertIn("goes backward", html)
        self.assertNotIn("movePage(this)", html)

    def test_a_pair_that_is_not_adjacent_gets_no_button(self):
        # 4 printed on page 3, a lettered page between (which breaks no
        # sequence, #319), 3 printed on page 5: a backward step of one,
        # but the two pages are not neighbours.
        scan = self._scan(["1", "2", "4", "10a", "3", "5"])
        for entry in scan.ocr_results:
            if entry["detected"] == "10a":
                entry["type"] = page_numbers.SUFFIXED
        scan.save(update_fields=["ocr_results"])
        html = self._step_one(scan).content.decode()

        self.assertIn("goes backward", html)
        self.assertNotIn("movePage(this)", html)

    def test_after_the_move_the_page_lists_the_corrected_order(self):
        self._post("move_page", pdf_page=4, anchor_pdf_page=2)

        html = self._step_one().content.decode()

        self.assertNotIn("movePage(this)", html)
        self.assertNotIn(">ORDER<", html)
        self.assertEqual(html.count("page-moved-badge"), 1)
        self.assertIn('movedPages: {"4": 2}', html)
        sidebar = html[html.index('id="pages-list"') :]
        rows = [int(m) for m in re.findall(r'data-pdf-index="(\d+)"', sidebar)]
        self.assertEqual(rows[:6], [0, 1, 3, 2, 4, 5])

    def test_a_printed_number_two_pairs_share_gets_no_button(self):
        # A repeated printed number, the defect review 1 exists to find:
        # both cards name 3, and the pair of either would move the
        # other's page.
        scan = self._scan(["1", "2", "4", "3", "5", "6", "4", "3", "8"])
        html = self._step_one(scan).content.decode()

        # Two cards for 3, and one for the 4 that follows 6.
        self.assertEqual(html.count('data-check="backward_page"'), 3)
        self.assertNotIn("movePage(this)", html)

    def test_a_locked_volume_offers_no_swap(self):
        Scan.objects.filter(pk=self.scan.pk).update(
            status=Status.PAGE_COMPLETENESS_REVIEW_DONE
        )
        self.scan.refresh_from_db()
        html = self._step_one().content.decode()

        self.assertIn("goes backward", html)
        self.assertNotIn("movePage(this)", html)
