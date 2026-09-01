"""Tests for the one model that holds a human page edit (issue #214).

This module covers the storage and the address space: the constraints
that keep one decision at one address, and the image key. The readers
that overlay these rows live in ``test_services.py`` and
``test_page_numbers.py``; the endpoints that write them live in
``test_views.py``.
"""

import json
import pathlib
import tempfile

import fitz
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

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

    def test_an_undo_removes_the_row(self):
        self._post("delete_page", 2)

        self.assertEqual(self._post("undo_delete_page", 2).status_code, 200)

        self.assertFalse(self.scan.page_edits.exists())

    def test_a_page_the_volume_does_not_have_is_refused(self):
        self.assertEqual(self._post("delete_page", 9).status_code, 404)
        self.assertFalse(self.scan.page_edits.exists())


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
        self.assertFalse(self.scan.page_edits.exists())

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

    def test_a_second_replacement_of_one_page_updates_its_row(self):
        for _ in range(2):
            self.client.post(
                reverse("replace_page", kwargs={"pk": self.scan.pk}),
                data={"image": self.make_image(), "pdf_page": 2},
            )
        self.assertEqual(self.scan.page_edits.count(), 1)

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

    def test_an_applied_edit_is_not_applied_again(self):
        PageEditFactory(
            scan=self.scan,
            kind=PageEdit.Kind.DELETE_PAGE,
            pdf_page=2,
            value="",
            applied_at=timezone.now(),
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
