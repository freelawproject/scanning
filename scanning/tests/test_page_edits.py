"""Tests for the one model that holds a human page edit (issue #214).

This module covers the storage and the address space: the constraints
that keep one decision at one address, and the image key. The readers
that overlay these rows live in ``test_services.py`` and
``test_page_numbers.py``; the endpoints that write them live in
``test_views.py``.
"""

import tempfile

from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from scanning.factories import PageEditFactory, ScanFactory
from scanning.models import PageEdit

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
