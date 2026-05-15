import io
import json
import os
import pathlib
import shutil
import tempfile
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import models
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from scanning.factories import (
    OpinionScanFactory,
    ReporterFactory,
    ScanFactory,
    UserFactory,
)
from scanning.models import (
    Detection,
    OpinionScan,
    OpinionStatus,
    Scan,
    Source,
    Stage,
    Status,
)

MEDIA_ROOT = tempfile.mkdtemp()


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class ScanningTestCase(TestCase):
    """Base test case with shared helper methods.

    Uses a temporary directory for MEDIA_ROOT so uploaded files
    don't accumulate in the real media directory.
    """

    @classmethod
    def tearDownClass(cls):
        """Remove the temporary media directory after all tests."""
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
        super().tearDownClass()

    def make_user(self, **kwargs):
        """Create a regular user via factory.

        :param kwargs: Overrides for UserFactory.
        :returns: A User instance.
        :rtype: User
        """
        return UserFactory(**kwargs)

    def make_staff_user(self, **kwargs):
        """Create a staff user via factory.

        :param kwargs: Overrides for UserFactory.
        :returns: A User instance with is_staff=True.
        :rtype: User
        """
        return UserFactory(is_staff=True, **kwargs)

    @staticmethod
    def make_pdf(filename="test.pdf"):
        """Create a minimal PDF file for upload testing.

        :param filename: The filename.
        :type filename: str
        :returns: A SimpleUploadedFile containing PDF data.
        :rtype: SimpleUploadedFile
        """
        return SimpleUploadedFile(
            filename,
            b"%PDF-1.4 test content",
            content_type="application/pdf",
        )

    @staticmethod
    def make_image():
        """Create a minimal image file for upload testing.

        :returns: A SimpleUploadedFile containing PNG image data.
        :rtype: SimpleUploadedFile
        """
        img = Image.new("RGB", (10, 10), color="red")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return SimpleUploadedFile(
            "cover.png",
            buf.read(),
            content_type="image/png",
        )


class TestAuthentication(ScanningTestCase):
    """Test login page and redirect safety."""

    def test_login_page_renders(self):
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sign In")

    def test_login_success(self):
        user = self.make_user()
        response = self.client.post(
            reverse("login"),
            {"username": user.username, "password": "testpass123"},
        )
        self.assertEqual(response.status_code, 302)

    def test_login_rejects_open_redirect(self):
        user = self.make_user()
        response = self.client.post(
            reverse("login") + "?next=https://evil.com",
            {"username": user.username, "password": "testpass123"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("evil.com", response.url)


class TestScanList(ScanningTestCase):
    """Test the scan list view."""

    def test_shows_all_scans(self):
        user = self.make_user()
        self.client.force_login(user)
        ScanFactory(uploaded_by=user)
        ScanFactory()  # another user's scan
        response = self.client.get(reverse("scan_list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["page_obj"]), 2)

    def test_filter_by_status(self):
        user = self.make_user()
        self.client.force_login(user)
        ScanFactory(uploaded_by=user, status=Status.UPLOADED)
        ScanFactory(uploaded_by=user, status=Status.APPROVED)
        response = self.client.get(
            reverse("scan_list"), {"status": Status.UPLOADED}
        )
        self.assertEqual(len(response.context["page_obj"]), 1)

    def test_filter_by_reporter(self):
        user = self.make_user()
        self.client.force_login(user)
        reporter_a = ReporterFactory(
            short_name="a", full_name="Atlantic Reporter"
        )
        reporter_f3d = ReporterFactory(
            short_name="f3d", full_name="Federal Reporter, 3d"
        )
        ScanFactory(uploaded_by=user, reporter=reporter_a)
        ScanFactory(uploaded_by=user, reporter=reporter_f3d)
        response = self.client.get(
            reverse("scan_list"), {"reporter": reporter_a.pk}
        )
        self.assertEqual(len(response.context["page_obj"]), 1)

    def test_pagination(self):
        user = self.make_user()
        self.client.force_login(user)
        ScanFactory.create_batch(30, uploaded_by=user)
        response = self.client.get(reverse("scan_list"))
        self.assertEqual(len(response.context["page_obj"]), 25)
        response = self.client.get(reverse("scan_list"), {"page": 2})
        self.assertEqual(len(response.context["page_obj"]), 5)


class TestScanDetail(ScanningTestCase):
    """Test the scan detail view."""

    def test_renders_detail(self):
        user = self.make_user()
        self.client.force_login(user)
        scan = ScanFactory(uploaded_by=user)
        response = self.client.get(
            reverse("scan_detail", kwargs={"pk": scan.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, scan.reporter.full_name)

    def test_non_staff_sees_review_form(self):
        user = self.make_user()
        self.client.force_login(user)
        scan = ScanFactory(uploaded_by=user)
        response = self.client.get(
            reverse("scan_detail", kwargs={"pk": scan.pk})
        )
        self.assertIsNotNone(response.context["review_form"])

    def test_can_view_other_users_scan(self):
        user = self.make_user()
        self.client.force_login(user)
        scan = ScanFactory()  # different user
        response = self.client.get(
            reverse("scan_detail", kwargs={"pk": scan.pk})
        )
        self.assertEqual(response.status_code, 200)

    def test_shows_only_original_pdf(self):
        user = self.make_user()
        self.client.force_login(user)
        scan = ScanFactory(uploaded_by=user)
        response = self.client.get(
            reverse("scan_detail", kwargs={"pk": scan.pk})
        )
        self.assertContains(response, "Original PDF")
        self.assertNotContains(response, "Redacted PDF")


class TestStaffReview(ScanningTestCase):
    """Test staff review functionality."""

    def test_staff_sees_review_form(self):
        staff = self.make_staff_user()
        self.client.force_login(staff)
        scan = ScanFactory()
        response = self.client.get(
            reverse("scan_detail", kwargs={"pk": scan.pk})
        )
        self.assertIsNotNone(response.context["review_form"])

    def test_approve_sets_processed_at(self):
        staff = self.make_staff_user()
        self.client.force_login(staff)
        scan = ScanFactory()
        response = self.client.post(
            reverse("scan_detail", kwargs={"pk": scan.pk}),
            {"status": Status.APPROVED, "notes": "Looks good"},
        )
        self.assertEqual(response.status_code, 302)
        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.APPROVED)
        self.assertIsNotNone(scan.processed_at)
        self.assertEqual(scan.notes, "Looks good")

    def test_reject_resets_status(self):
        staff = self.make_staff_user()
        self.client.force_login(staff)
        scan = ScanFactory(status=Status.PENDING_REVIEW)
        response = self.client.post(
            reverse("scan_detail", kwargs={"pk": scan.pk}),
            {"status": Status.UPLOADED, "notes": "Needs rescanning"},
        )
        self.assertEqual(response.status_code, 302)
        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.UPLOADED)
        self.assertIsNone(scan.processed_at)

    def test_approved_scan_hides_review_form(self):
        staff = self.make_staff_user()
        self.client.force_login(staff)
        scan = ScanFactory(status=Status.APPROVED)
        response = self.client.get(
            reverse("scan_detail", kwargs={"pk": scan.pk})
        )
        self.assertIsNone(response.context["review_form"])
        self.assertContains(response, "Review Decision")
        self.assertContains(response, "Approved")


class TestScanModel(ScanningTestCase):
    """Test the Scan model."""

    def test_upload_path_format(self):
        scan = ScanFactory(volume=5, start_page=1, end_page=100)
        self.assertTrue(scan.original_pdf.name.startswith("original_scans/a/"))
        self.assertTrue(scan.original_pdf.name.endswith(".pdf"))

    def test_book_upload_path_contains_pages(self):
        scan = ScanFactory(volume=3, start_page=10, end_page=200)
        self.assertTrue(
            scan.original_pdf.name.startswith("original_scans/a/3/10/")
        )
        self.assertTrue(scan.original_pdf.name.endswith(".original.pdf"))

    def test_book_upload_path_opinions_source(self):
        scan = ScanFactory(
            volume=3, start_page=10, end_page=200, source=Source.OPINIONS
        )
        self.assertTrue(
            scan.original_pdf.name.startswith("original_scans/a/3/10/")
        )
        self.assertTrue(scan.original_pdf.name.endswith(".original.pdf"))


class TestOpinionScanModel(ScanningTestCase):
    """Test the OpinionScan model."""

    def test_upload_path_format(self):
        opinion = OpinionScanFactory(volume=7)
        self.assertTrue(opinion.original_pdf.name.startswith("opinions/a/7/"))

    def test_protect_from_scan_delete(self):
        scan = ScanFactory()
        OpinionScanFactory(scan=scan, reporter=scan.reporter)
        with self.assertRaises(models.ProtectedError):
            scan.delete()
        self.assertEqual(OpinionScan.objects.count(), 1)


class TestAutoNowQuerySet(ScanningTestCase):
    """Test that AutoNowQuerySet stamps ``auto_now`` fields on ``.update()``.

    Without the custom queryset, ``QuerySet.update()`` bypasses the
    ``pre_save`` hooks that maintain ``auto_now`` fields, so
    ``date_modified`` never advances. This behavior is structural (on
    ``AbstractDateTimeModel``), so these tests also guard against the fix
    being accidentally removed.
    """

    def test_update_stamps_date_modified_on_scan(self):
        """Bulk .update() advances date_modified on Scan."""
        scan = ScanFactory()
        old = timezone.now() - timedelta(days=7)
        # Seed an old timestamp (caller-provided value is respected).
        Scan.objects.filter(pk=scan.pk).update(date_modified=old)
        # A subsequent update with no date_modified kwarg should re-stamp it.
        Scan.objects.filter(pk=scan.pk).update(progress_message="bumped")
        scan.refresh_from_db()
        self.assertGreater(scan.date_modified, old)

    def test_update_stamps_date_modified_on_detection(self):
        """Detection inherits the manager through AbstractDateTimeModel."""
        scan = ScanFactory()
        det = Detection.objects.create(
            scan=scan,
            page_index=0,
            label="KEY",
            label_id=0,
            confidence=0.5,
            x0=0,
            y0=0,
            x1=10,
            y1=10,
            img_width=100,
            img_height=100,
        )
        old = timezone.now() - timedelta(days=7)
        Detection.objects.filter(pk=det.pk).update(date_modified=old)
        Detection.objects.filter(pk=det.pk).update(confidence=1.0)
        det.refresh_from_db()
        self.assertGreater(det.date_modified, old)

    def test_update_respects_explicit_date_modified(self):
        """Caller-supplied date_modified is not overridden (setdefault)."""
        scan = ScanFactory()
        explicit = timezone.now() - timedelta(days=3)
        Scan.objects.filter(pk=scan.pk).update(date_modified=explicit)
        scan.refresh_from_db()
        self.assertAlmostEqual(
            scan.date_modified.timestamp(),
            explicit.timestamp(),
            delta=1,
        )

    def test_save_update_fields_stamps_date_modified(self):
        """``save(update_fields=[...])`` still advances ``date_modified``.

        Django's stock ``save(update_fields=[...])`` silently skips
        ``auto_now`` fields, so ``AbstractDateTimeModel.save`` auto-adds
        them to ``update_fields``. Without that override,
        ``scan.save(update_fields=["status"])`` would leave
        ``date_modified`` unchanged.
        """
        scan = ScanFactory()
        old = timezone.now() - timedelta(days=7)
        Scan.objects.filter(pk=scan.pk).update(date_modified=old)
        scan.refresh_from_db()
        scan.status = Status.CANCELLED
        scan.save(update_fields=["status"])
        scan.refresh_from_db()
        self.assertGreater(scan.date_modified, old)

    def test_bulk_update_stamps_date_modified(self):
        """``bulk_update()`` stamps ``date_modified`` on every instance."""
        scan1 = ScanFactory()
        scan2 = ScanFactory()
        old = timezone.now() - timedelta(days=7)
        Scan.objects.filter(pk__in=[scan1.pk, scan2.pk]).update(
            date_modified=old
        )
        scan1.refresh_from_db()
        scan2.refresh_from_db()

        scan1.status = Status.QUEUED
        scan2.status = Status.PROCESSING
        Scan.objects.bulk_update([scan1, scan2], ["status"])

        scan1.refresh_from_db()
        scan2.refresh_from_db()
        self.assertGreater(scan1.date_modified, old)
        self.assertGreater(scan2.date_modified, old)


class TestOpinionList(ScanningTestCase):
    """Test the opinion list view."""

    def test_renders_list(self):
        user = self.make_user()
        self.client.force_login(user)
        OpinionScanFactory(uploaded_by=user)
        response = self.client.get(reverse("opinion_list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["page_obj"]), 1)

    def test_filter_by_scan(self):
        user = self.make_user()
        self.client.force_login(user)
        scan = ScanFactory(uploaded_by=user)
        OpinionScanFactory(scan=scan, reporter=scan.reporter)
        OpinionScanFactory()  # standalone
        response = self.client.get(reverse("opinion_list"), {"scan": scan.pk})
        self.assertEqual(len(response.context["page_obj"]), 1)

    def test_filter_by_reporter(self):
        user = self.make_user()
        self.client.force_login(user)
        reporter_a = ReporterFactory(
            short_name="a", full_name="Atlantic Reporter"
        )
        reporter_f3d = ReporterFactory(
            short_name="f3d", full_name="Federal Reporter, 3d"
        )
        OpinionScanFactory(reporter=reporter_a)
        OpinionScanFactory(reporter=reporter_f3d)
        response = self.client.get(
            reverse("opinion_list"), {"reporter": reporter_a.pk}
        )
        self.assertEqual(len(response.context["page_obj"]), 1)

    def test_filter_by_status(self):
        user = self.make_user()
        self.client.force_login(user)
        OpinionScanFactory(status=OpinionStatus.OK)
        OpinionScanFactory(status=OpinionStatus.GAP)
        response = self.client.get(
            reverse("opinion_list"), {"status": OpinionStatus.GAP}
        )
        self.assertEqual(len(response.context["page_obj"]), 1)

    def test_pagination(self):
        user = self.make_user()
        self.client.force_login(user)
        OpinionScanFactory.create_batch(60, uploaded_by=user)
        response = self.client.get(reverse("opinion_list"))
        self.assertEqual(len(response.context["page_obj"]), 50)
        response = self.client.get(reverse("opinion_list"), {"page": 2})
        self.assertEqual(len(response.context["page_obj"]), 10)


class TestOpinionDetail(ScanningTestCase):
    """Test the opinion detail view."""

    def test_renders_detail(self):
        user = self.make_user()
        self.client.force_login(user)
        opinion = OpinionScanFactory(uploaded_by=user)
        response = self.client.get(
            reverse("opinion_detail", kwargs={"pk": opinion.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, opinion.reporter.full_name)

    def test_shows_parent_scan_link(self):
        user = self.make_user()
        self.client.force_login(user)
        scan = ScanFactory(uploaded_by=user)
        opinion = OpinionScanFactory(scan=scan, reporter=scan.reporter)
        response = self.client.get(
            reverse("opinion_detail", kwargs={"pk": opinion.pk})
        )
        self.assertContains(response, "Book")
        self.assertContains(
            response, reverse("scan_detail", kwargs={"pk": scan.pk})
        )

    def test_standalone_opinion_no_parent_link(self):
        user = self.make_user()
        self.client.force_login(user)
        opinion = OpinionScanFactory(uploaded_by=user)
        response = self.client.get(
            reverse("opinion_detail", kwargs={"pk": opinion.pk})
        )
        self.assertNotContains(response, "Parent Book")


class TestOpinionUpload(ScanningTestCase):
    """Test the opinion upload functionality (superuser only)."""

    def make_superuser(self, **kwargs):
        """Create a superuser via factory.

        :param kwargs: Overrides for UserFactory.
        :returns: A User instance with is_superuser=True.
        :rtype: User
        """
        return UserFactory(is_superuser=True, **kwargs)

    def test_regular_user_forbidden(self):
        self.client.force_login(self.make_user())
        response = self.client.get(reverse("opinion_upload"))
        self.assertEqual(response.status_code, 403)

    def test_upload_page_renders(self):
        self.client.force_login(self.make_superuser())
        response = self.client.get(reverse("opinion_upload"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Upload Opinion Scan")

    def test_successful_upload(self):
        user = self.make_superuser()
        self.client.force_login(user)
        reporter = ReporterFactory()
        response = self.client.post(
            reverse("opinion_upload"),
            {
                "reporter": reporter.pk,
                "volume": 5,
                "original_pdf": self.make_pdf("opinion.pdf"),
                "status": OpinionStatus.NO_STATUS,
                "page_start": 1,
                "page_end": 10,
            },
        )
        self.assertEqual(response.status_code, 302)
        opinion = OpinionScan.objects.get()
        self.assertEqual(opinion.uploaded_by, user)
        self.assertEqual(opinion.reporter, reporter)
        self.assertEqual(opinion.volume, 5)
        self.assertIsNone(opinion.scan)

    def test_upload_missing_pdf(self):
        self.client.force_login(self.make_superuser())
        reporter = ReporterFactory()
        response = self.client.post(
            reverse("opinion_upload"),
            {
                "reporter": reporter.pk,
                "volume": 1,
                "status": OpinionStatus.NO_STATUS,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(OpinionScan.objects.count(), 0)

    def test_upload_rejects_non_pdf_mime_type(self):
        self.client.force_login(self.make_superuser())
        reporter = ReporterFactory()
        fake_pdf = SimpleUploadedFile(
            "fake.pdf",
            b"not a pdf",
            content_type="text/plain",
        )
        response = self.client.post(
            reverse("opinion_upload"),
            {
                "reporter": reporter.pk,
                "volume": 1,
                "original_pdf": fake_pdf,
                "status": OpinionStatus.NO_STATUS,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(OpinionScan.objects.count(), 0)


class TestOpinionScanValidation(ScanningTestCase):
    """Test OpinionScan model validation."""

    def test_page_start_greater_than_page_end_raises(self):
        opinion = OpinionScanFactory.build(page_start=20, page_end=10)
        with self.assertRaises(ValidationError) as cm:
            opinion.full_clean()
        self.assertIn("page_end", cm.exception.message_dict)

    def test_page_start_equal_to_page_end_ok(self):
        opinion = OpinionScanFactory(page_start=5, page_end=5)
        opinion.full_clean()  # should not raise


class TestScanListFilters(ScanningTestCase):
    """Test that invalid filter params don't crash scan_list."""

    def test_invalid_reporter_filter_returns_200(self):
        self.client.force_login(self.make_user())
        response = self.client.get(
            reverse("scan_list"), {"reporter": "notanumber"}
        )
        self.assertEqual(response.status_code, 200)


class TestOpinionListFilters(ScanningTestCase):
    """Test that invalid filter params don't crash opinion_list."""

    def test_invalid_reporter_filter_returns_200(self):
        self.client.force_login(self.make_user())
        response = self.client.get(
            reverse("opinion_list"), {"reporter": "notanumber"}
        )
        self.assertEqual(response.status_code, 200)

    def test_invalid_scan_filter_returns_200(self):
        self.client.force_login(self.make_user())
        response = self.client.get(
            reverse("opinion_list"), {"scan": "notanumber"}
        )
        self.assertEqual(response.status_code, 200)


class TestScanValidation(ScanningTestCase):
    """Test Scan model validation."""

    def test_start_page_greater_than_end_page_raises(self):
        scan = ScanFactory.build(start_page=50, end_page=10)
        with self.assertRaises(ValidationError) as cm:
            scan.full_clean()
        self.assertIn("end_page", cm.exception.message_dict)

    def test_zero_volume_rejected(self):
        scan = ScanFactory.build(volume=0)
        with self.assertRaises(ValidationError) as cm:
            scan.full_clean()
        self.assertIn("volume", cm.exception.message_dict)

    def test_zero_start_page_rejected(self):
        scan = ScanFactory.build(start_page=0)
        with self.assertRaises(ValidationError) as cm:
            scan.full_clean()
        self.assertIn("start_page", cm.exception.message_dict)

    def test_number_of_pages_less_than_range_rejected(self):
        scan = ScanFactory.build(number_of_pages=3, start_page=1, end_page=50)
        with self.assertRaises(ValidationError) as cm:
            scan.full_clean()
        self.assertIn("number_of_pages", cm.exception.message_dict)

    def test_number_of_pages_equal_to_range_ok(self):
        scan = ScanFactory(number_of_pages=50, start_page=1, end_page=50)
        scan.full_clean()  # should not raise

    def test_allows_same_reporter_volume_source(self):
        reporter = ReporterFactory()
        ScanFactory(reporter=reporter, volume=1, source=Source.FULL)
        ScanFactory(
            reporter=reporter, volume=1, source=Source.FULL
        )  # no unique constraint anymore


class TestSpoofedPdfUpload(ScanningTestCase):
    """Test that spoofed PDF files (correct MIME, wrong content) are rejected."""

    def test_opinion_upload_spoofed_pdf_rejected(self):
        user = UserFactory(is_superuser=True)
        self.client.force_login(user)
        reporter = ReporterFactory()
        spoofed = SimpleUploadedFile(
            "fake.pdf",
            b"<html>not a pdf</html>",
            content_type="application/pdf",
        )
        response = self.client.post(
            reverse("opinion_upload"),
            {
                "reporter": reporter.pk,
                "volume": 1,
                "original_pdf": spoofed,
                "status": OpinionStatus.NO_STATUS,
                "page_start": 1,
                "page_end": 10,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(OpinionScan.objects.count(), 0)


class TestOpinionListEmptyState(ScanningTestCase):
    """Test that the opinion list empty state respects permissions."""

    def test_non_superuser_no_upload_link(self):
        self.client.force_login(self.make_user())
        response = self.client.get(reverse("opinion_list"))
        self.assertNotContains(response, "Upload your first opinion")


class TestProfile(ScanningTestCase):
    """Test the user profile view."""

    def test_login_required(self):
        response = self.client.get(reverse("profile"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    def test_get_renders_form(self):
        self.client.force_login(self.make_user())
        response = self.client.get(reverse("profile"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Profile")

    def test_post_updates_fields(self):
        user = self.make_user()
        self.client.force_login(user)
        response = self.client.post(
            reverse("profile"),
            {
                "first_name": "Jane",
                "last_name": "Doe",
                "email": "jane@example.com",
            },
        )
        self.assertEqual(response.status_code, 302)
        user.refresh_from_db()
        self.assertEqual(user.first_name, "Jane")
        self.assertEqual(user.last_name, "Doe")
        self.assertEqual(user.email, "jane@example.com")


class TestPasswordChange(ScanningTestCase):
    """Test the password change view."""

    def test_login_required(self):
        response = self.client.get(reverse("password_change"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    def test_get_renders_form(self):
        self.client.force_login(self.make_user())
        response = self.client.get(reverse("password_change"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Change Password")

    def test_post_valid_passwords_redirects(self):
        user = self.make_user()
        self.client.force_login(user)
        response = self.client.post(
            reverse("password_change"),
            {
                "old_password": "testpass123",
                "new_password1": "newSecurePass456!",
                "new_password2": "newSecurePass456!",
            },
        )
        self.assertRedirects(response, reverse("profile"))
        user.refresh_from_db()
        self.assertTrue(user.check_password("newSecurePass456!"))

    def test_post_wrong_old_password_fails(self):
        user = self.make_user()
        self.client.force_login(user)
        response = self.client.post(
            reverse("password_change"),
            {
                "old_password": "wrongpass",
                "new_password1": "newSecurePass456!",
                "new_password2": "newSecurePass456!",
            },
        )
        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertTrue(user.check_password("testpass123"))


class TestMonitoring(TestCase):
    """Test the monitoring endpoints."""

    def test_heartbeat_returns_ok(self):
        response = self.client.get(reverse("heartbeat"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"OK")
        self.assertEqual(response["Content-Type"], "text/plain")

    def test_health_check_returns_json(self):
        response = self.client.get(reverse("health_check"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("is_postgresql_up", data)
        self.assertTrue(data["is_postgresql_up"])


class TestApproveScan(ScanningTestCase):
    """Test the approve_scan view."""

    def _make_scan_with_generated_files(self):
        """Create a scan that has been through file generation."""
        import pathlib

        scan = ScanFactory(start_page=1, end_page=95, stage=Stage.APPROVED)
        output = pathlib.Path(scan.output_dir)
        output.mkdir(parents=True, exist_ok=True)

        redacted_dir = output / "redacted"
        masked_dir = output / "masked"
        redacted_dir.mkdir()
        masked_dir.mkdir()
        (redacted_dir / "a.1.0001-0010.pdf").write_bytes(b"%PDF-1.4")
        (masked_dir / "a.1.0001-0010.pdf").write_bytes(b"%PDF-1.4")
        return scan

    def test_approve_without_files_shows_error(self):
        """Approving before reaching the APPROVED stage shows an error."""
        user = self.make_staff_user()
        self.client.force_login(user)
        scan = ScanFactory(start_page=1, end_page=95)

        response = self.client.post(
            reverse("approve_scan", kwargs={"pk": scan.pk}),
            follow=True,
        )
        self.assertContains(response, "Before approving")
        scan.refresh_from_db()
        self.assertFalse(scan.s3_uploaded)

    def test_approve_with_files_sets_path(self):
        """Approving with generated files sets s3_path."""
        from unittest.mock import patch

        user = self.make_staff_user()
        self.client.force_login(user)
        scan = self._make_scan_with_generated_files()

        with patch.dict("os.environ", {}, clear=True):
            self.client.post(
                reverse("approve_scan", kwargs={"pk": scan.pk}),
                follow=True,
            )
        scan.refresh_from_db()
        self.assertTrue(scan.s3_path.startswith("approved/"))

    @override_settings(AWS_PRIVATE_STORAGE_BUCKET_NAME="test-bucket")
    def test_approve_with_creds_sets_uploaded_flag(self):
        """Approving with AWS creds sets s3_uploaded to True."""
        from unittest.mock import MagicMock, patch

        from botocore.exceptions import ClientError

        user = self.make_staff_user()
        self.client.force_login(user)
        scan = self._make_scan_with_generated_files()

        mock_client = MagicMock()
        mock_client.head_object.side_effect = ClientError(
            {"Error": {"Code": "404"}}, "HeadObject"
        )
        mock_env = {
            "AWS_ACCESS_KEY_ID": "test",
            "AWS_SECRET_ACCESS_KEY": "test",
        }
        with (
            patch.dict("os.environ", mock_env),
            patch("boto3.client", return_value=mock_client),
        ):
            self.client.post(
                reverse("approve_scan", kwargs={"pk": scan.pk}),
                follow=True,
            )
        scan.refresh_from_db()
        self.assertTrue(scan.s3_uploaded)
        self.assertTrue(scan.s3_path.startswith("approved/"))

    def test_approve_requires_login(self):
        """Approve endpoint redirects to login for anonymous users."""
        scan = ScanFactory()
        response = self.client.post(
            reverse("approve_scan", kwargs={"pk": scan.pk})
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_approve_requires_post(self):
        """Approve endpoint rejects GET requests."""
        user = self.make_staff_user()
        self.client.force_login(user)
        scan = ScanFactory()
        response = self.client.get(
            reverse("approve_scan", kwargs={"pk": scan.pk})
        )
        self.assertEqual(response.status_code, 405)


class TestQueueUploadS3(ScanningTestCase):
    """Upload view pushes originals to S3 in prod, MEDIA_ROOT in dev."""

    def _post_upload(self):
        """Helper: create Volume + post a valid PDF upload.

        :returns: The HTTP response.
        """

        from scanning.models import Volume

        user = self.make_user()
        self.client.force_login(user)
        reporter = ReporterFactory(short_name="tu")
        volume = Volume.objects.create(
            reporter=reporter,
            volume_number=42,
        )
        pdf = SimpleUploadedFile(
            "scan.pdf", b"%PDF-1.4 body", content_type="application/pdf"
        )
        return self.client.post(
            reverse(
                "queue_upload",
                kwargs={"reporter_slug": "tu", "vol": 42},
            ),
            {
                "new_scan": "1",
                "first_page": "1",
                "last_page": "10",
                "original_pdf": pdf,
            },
        ), volume

    @override_settings(DEVELOPMENT=True)
    def test_dev_writes_filefield_and_skips_s3(self):
        from unittest.mock import patch

        with patch("scanning.s3_sync.upload_file_to_s3") as mock_s3:
            response, _ = self._post_upload()

        self.assertEqual(response.status_code, 302)
        mock_s3.assert_not_called()
        scan = Scan.objects.get()
        # FileField has a real file (ends with .original.pdf).
        self.assertTrue(scan.original_pdf.name.endswith(".original.pdf"))
        # Django's FileField wrote an actual file to storage.
        self.assertTrue(
            scan.original_pdf.storage.exists(scan.original_pdf.name)
        )

    @override_settings(DEVELOPMENT=False, TESTING=False)
    def test_prod_uploads_to_s3_and_skips_filefield(self):
        from unittest.mock import patch

        with (
            patch("scanning.views.has_s3_credentials", return_value=True),
            patch(
                "scanning.s3_sync.upload_file_to_s3", return_value=True
            ) as mock_s3,
        ):
            response, _ = self._post_upload()

        self.assertEqual(response.status_code, 302)
        mock_s3.assert_called_once()
        scan = Scan.objects.get()
        self.assertTrue(scan.original_pdf.name.endswith(".original.pdf"))
        # FileField was NOT written to MEDIA_ROOT, only metadata set.
        self.assertFalse(
            scan.original_pdf.storage.exists(scan.original_pdf.name)
        )

    @override_settings(DEVELOPMENT=False, TESTING=False)
    def test_prod_s3_exception_deletes_scan_and_shows_error(self):
        from unittest.mock import patch

        with (
            patch("scanning.views.has_s3_credentials", return_value=True),
            patch(
                "scanning.s3_sync.upload_file_to_s3",
                side_effect=RuntimeError("boom"),
            ),
            self.assertLogs("scanning.views", level="ERROR") as cm,
        ):
            response, _ = self._post_upload()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Scan.objects.count(), 0)
        self.assertTrue(
            any("Failed to upload original PDF" in m for m in cm.output)
        )

    @override_settings(DEVELOPMENT=False, TESTING=False)
    def test_prod_missing_creds_deletes_scan_early(self):
        from unittest.mock import patch

        with (
            patch("scanning.views.has_s3_credentials", return_value=False),
            patch("scanning.s3_sync.upload_file_to_s3") as mock_s3,
            self.assertLogs("scanning.views", level="ERROR") as cm,
        ):
            response, _ = self._post_upload()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Scan.objects.count(), 0)
        mock_s3.assert_not_called()
        self.assertTrue(any("without AWS credentials" in m for m in cm.output))

    @override_settings(DEVELOPMENT=False, TESTING=False)
    def test_prod_upload_returning_false_deletes_scan(self):
        from unittest.mock import patch

        # upload_file_to_s3 returning False (e.g. the helper's own
        # short-circuit) should be treated as a failure by the view.
        with (
            patch("scanning.views.has_s3_credentials", return_value=True),
            patch("scanning.s3_sync.upload_file_to_s3", return_value=False),
        ):
            response, _ = self._post_upload()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Scan.objects.count(), 0)


class TestPdfPathFallback(ScanningTestCase):
    """Scan.pdf_path raises FileNotFoundError in prod when no local file."""

    @override_settings(DEVELOPMENT=False, TESTING=False)
    def test_prod_raises_when_no_local_file(self):
        scan = ScanFactory(original_pdf=None)
        scan.original_pdf.name = "nothing.pdf"
        scan.save(update_fields=["original_pdf"])
        with self.assertRaises(FileNotFoundError):
            _ = scan.pdf_path

    @override_settings(DEVELOPMENT=True)
    def test_dev_falls_back_to_filefield_path(self):
        # ScanFactory populates original_pdf via FileField, so .path works.
        scan = ScanFactory()
        self.assertTrue(scan.pdf_path.endswith(".pdf"))


class TestServeOpinionPdfLazyPull(ScanningTestCase):
    """serve_opinionscan_pdf falls back to S3 pull when /tmp/ is stale."""

    def test_lazy_pull_populates_tmp_and_serves(self):
        import pathlib
        import tempfile
        from unittest.mock import patch

        from scanning.models import OpinionScan, OpinionStatus

        user = self.make_user()
        self.client.force_login(user)

        # Create a scan + opinion whose redacted_pdf.name is relative to
        # the scan's output_dir (prod-style layout).
        tmp_root = tempfile.mkdtemp()
        reporter = ReporterFactory(short_name="tp")
        scan = ScanFactory(
            reporter=reporter, volume=77, start_page=1, end_page=2
        )
        opinion = OpinionScan.objects.create(
            scan=scan,
            reporter=reporter,
            volume=77,
            opinion_order=0,
            page_start=1,
            page_end=2,
            status=OpinionStatus.OK,
            uploaded_by=user,
        )
        opinion.redacted_pdf.name = "redacted/op.pdf"
        opinion.save(update_fields=["redacted_pdf"])

        def _fake_download(scan_arg):
            # Simulate the pull writing the file to the scan's output dir.
            target = pathlib.Path(scan_arg.output_dir) / "redacted" / "op.pdf"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"%PDF-1.4 pulled")

        with (
            override_settings(
                DEVELOPMENT=False,
                TESTING=False,
                PROCESSING_TMP_DIR=tmp_root,
            ),
            patch(
                "scanning.s3_sync.download_processing_files",
                side_effect=_fake_download,
            ) as mock_pull,
        ):
            response = self.client.get(
                reverse(
                    "serve_opinionscan_pdf",
                    kwargs={"pk": opinion.pk, "variant": "redacted"},
                )
            )

        self.assertEqual(response.status_code, 200)
        mock_pull.assert_called_once()


class TestServeOpinionPdfCaching(ScanningTestCase):
    """serve_opinionscan_pdf sets cache headers and honors revalidation."""

    def test_response_carries_cache_headers(self):
        user = self.make_user()
        self.client.force_login(user)
        opinion = OpinionScanFactory()

        response = self.client.get(
            reverse(
                "serve_opinionscan_pdf",
                kwargs={"pk": opinion.pk, "variant": "original"},
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-cache")
        self.assertTrue(response["ETag"].startswith('"'))
        self.assertIn("Last-Modified", response)

    def test_if_none_match_returns_304(self):
        user = self.make_user()
        self.client.force_login(user)
        opinion = OpinionScanFactory()

        url = reverse(
            "serve_opinionscan_pdf",
            kwargs={"pk": opinion.pk, "variant": "original"},
        )
        first = self.client.get(url)
        etag = first["ETag"]

        second = self.client.get(url, HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(second.status_code, 304)
        self.assertEqual(second["ETag"], etag)
        self.assertEqual(second["Cache-Control"], "private, no-cache")


class TestServeScanPdfLazyPull(ScanningTestCase):
    """serve_scan_pdf falls back to S3 pull when /tmp/ is stale."""

    def test_lazy_pull_when_local_dir_missing(self):
        """When output_dir doesn't exist locally, pull from S3 and serve."""
        user = self.make_user()
        self.client.force_login(user)

        tmp_root = tempfile.mkdtemp()
        reporter = ReporterFactory(short_name="se2d")
        scan = ScanFactory(
            reporter=reporter, volume=921, start_page=290, end_page=350
        )
        # Simulate the prod web container: no local FileField copy, no
        # local /tmp/ dir. The only way to serve is via S3.
        original_path = scan.original_pdf.path
        if os.path.exists(original_path):
            os.remove(original_path)

        def _fake_download(scan_arg):
            output = pathlib.Path(scan_arg.output_dir)
            output.mkdir(parents=True, exist_ok=True)
            (output / "bitonal.pdf").write_bytes(b"%PDF-1.4 pulled")

        with (
            override_settings(
                DEVELOPMENT=False,
                TESTING=False,
                PROCESSING_TMP_DIR=tmp_root,
            ),
            patch(
                "scanning.s3_sync.download_processing_files",
                side_effect=_fake_download,
            ) as mock_pull,
        ):
            response = self.client.get(
                reverse("serve_scan_pdf", kwargs={"pk": scan.pk})
            )

        self.assertEqual(response.status_code, 200)
        mock_pull.assert_called_once()

    def test_returns_404_when_nothing_available(self):
        """When local is empty and S3 pull yields nothing, return 404."""
        user = self.make_user()
        self.client.force_login(user)

        tmp_root = tempfile.mkdtemp()
        scan = ScanFactory(start_page=1, end_page=2)
        # Delete the local FileField file so scan.pdf_path can't resolve
        # to the original upload either.
        original_path = scan.original_pdf.path
        if os.path.exists(original_path):
            os.remove(original_path)

        with (
            override_settings(
                DEVELOPMENT=False,
                TESTING=False,
                PROCESSING_TMP_DIR=tmp_root,
            ),
            patch("scanning.s3_sync.download_processing_files"),
        ):
            response = self.client.get(
                reverse("serve_scan_pdf", kwargs={"pk": scan.pk})
            )

        self.assertEqual(response.status_code, 404)


class TestRunFullPipelinePullsFromS3(ScanningTestCase):
    """run_full_pipeline pulls files from S3 at entry (prod only)."""

    def test_invokes_pull_helper(self):
        from unittest.mock import patch

        scan = ScanFactory(status=Status.QUEUED)
        with (
            # close_all() resets connections for daemon-process forking; in
            # tests it kills the test transaction -- patch it out.
            patch("django.db.connections.close_all"),
            patch(
                "scanning.services._pull_processing_files_from_s3"
            ) as mock_pull,
            # Stop the pipeline early by failing the first read.
            patch(
                "scanning.services.ensure_output_dir",
                side_effect=RuntimeError("stop"),
            ),
        ):
            from scanning.services import run_full_pipeline

            run_full_pipeline(scan.pk)

        mock_pull.assert_called_once_with(scan.pk)


class TestUpdateDetection(ScanningTestCase):
    """Tests for the update_detection endpoint."""

    def _make_scan_with_detection(self):
        """Create a scan, its output_dir, and one Detection record.

        :return: Tuple of (scan, detection).
        :rtype: tuple
        """
        scan = ScanFactory()
        output = pathlib.Path(scan.output_dir)
        output.mkdir(parents=True, exist_ok=True)
        det = Detection.objects.create(
            scan=scan,
            page_index=0,
            label="CASE_CAPTION",
            label_id=1,
            confidence=0.9,
            x0=100.0,
            y0=100.0,
            x1=200.0,
            y1=200.0,
            img_width=1200,
            img_height=1600,
            model_name=Detection.ModelName.SMALL,
            model_count=1,
            found_by=[{"model": "yolo", "confidence": 0.9}],
        )
        return scan, det

    def test_updates_db_and_disk(self):
        """POST with valid detection_id updates DB coords and writes detections.json."""
        from unittest.mock import patch

        user = self.make_staff_user()
        self.client.force_login(user)
        scan, det = self._make_scan_with_detection()

        with patch("scanning.s3_sync.upload_file_to_s3"):
            response = self.client.post(
                reverse("update_detection", kwargs={"pk": scan.pk}),
                data=json.dumps(
                    {
                        "detection_id": det.pk,
                        "new_bbox": [150.0, 150.0, 250.0, 250.0],
                    }
                ),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["updated"], 1)

        det.refresh_from_db()
        self.assertEqual(det.x0, 150.0)
        self.assertEqual(det.y0, 150.0)
        self.assertEqual(det.x1, 250.0)
        self.assertEqual(det.y1, 250.0)

        det_path = pathlib.Path(scan.output_dir) / "detections.json"
        self.assertTrue(det_path.exists())
        saved = json.loads(det_path.read_text())
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["bbox"], [150.0, 150.0, 250.0, 250.0])

    def test_unknown_detection_id_returns_404(self):
        """POST with a non-existent detection_id returns 404."""
        from unittest.mock import patch

        user = self.make_staff_user()
        self.client.force_login(user)
        scan, _det = self._make_scan_with_detection()

        with patch("scanning.s3_sync.upload_file_to_s3"):
            response = self.client.post(
                reverse("update_detection", kwargs={"pk": scan.pk}),
                data=json.dumps(
                    {
                        "detection_id": 999999,
                        "new_bbox": [0.0, 0.0, 10.0, 10.0],
                    }
                ),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 404)
        body = json.loads(response.content)
        self.assertEqual(body["status"], "error")


class TestDeleteDetection(ScanningTestCase):
    """Tests for the delete_detection endpoint."""

    def _make_scan_with_detection(self):
        """Create a scan, its output_dir, and one active Detection record.

        :return: Tuple of (scan, detection).
        :rtype: tuple
        """
        scan = ScanFactory()
        output = pathlib.Path(scan.output_dir)
        output.mkdir(parents=True, exist_ok=True)
        det = Detection.objects.create(
            scan=scan,
            page_index=0,
            label="CASE_CAPTION",
            label_id=1,
            confidence=0.9,
            x0=100.0,
            y0=100.0,
            x1=200.0,
            y1=200.0,
            img_width=1200,
            img_height=1600,
            model_name=Detection.ModelName.SMALL,
            model_count=1,
            found_by=[{"model": "small", "confidence": 0.9}],
        )
        return scan, det

    def test_deactivates_db_and_removes_from_disk(self):
        """POST with valid detection_id sets active=False and removes it from detections.json."""
        from unittest.mock import patch

        user = self.make_staff_user()
        self.client.force_login(user)
        scan, det = self._make_scan_with_detection()

        with patch("scanning.s3_sync.upload_file_to_s3"):
            response = self.client.post(
                reverse("delete_detection", kwargs={"pk": scan.pk}),
                data=json.dumps({"detection_id": det.pk}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["deleted"], 1)

        det.refresh_from_db()
        self.assertFalse(det.active)

        det_path = pathlib.Path(scan.output_dir) / "detections.json"
        self.assertTrue(det_path.exists())
        saved = json.loads(det_path.read_text())
        self.assertEqual(len(saved), 0)

    def test_unknown_id_returns_404(self):
        """POST with a non-existent detection_id returns 404."""
        from unittest.mock import patch

        user = self.make_staff_user()
        self.client.force_login(user)
        scan, _det = self._make_scan_with_detection()

        with patch("scanning.s3_sync.upload_file_to_s3"):
            response = self.client.post(
                reverse("delete_detection", kwargs={"pk": scan.pk}),
                data=json.dumps({"detection_id": 999999}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 404)
        body = json.loads(response.content)
        self.assertEqual(body["status"], "error")


class TestApproveDetection(ScanningTestCase):
    """Tests for the approve_detection endpoint."""

    def _make_scan_with_detection(self):
        """Create a scan, its output_dir, and one Detection record.

        :return: Tuple of (scan, detection).
        :rtype: tuple
        """
        scan = ScanFactory()
        output = pathlib.Path(scan.output_dir)
        output.mkdir(parents=True, exist_ok=True)
        det = Detection.objects.create(
            scan=scan,
            page_index=0,
            label="KEY_ICON",
            label_id=2,
            confidence=0.7,
            x0=50.0,
            y0=50.0,
            x1=100.0,
            y1=100.0,
            img_width=1200,
            img_height=1600,
            model_name=Detection.ModelName.SMALL,
            model_count=1,
            found_by=[{"model": "small", "confidence": 0.7}],
        )
        return scan, det

    def test_sets_confidence_and_syncs_disk(self):
        """POST with valid detection_id sets confidence=1.0 and updates detections.json."""
        from unittest.mock import patch

        user = self.make_staff_user()
        self.client.force_login(user)
        scan, det = self._make_scan_with_detection()

        with patch("scanning.s3_sync.upload_file_to_s3"):
            response = self.client.post(
                reverse("approve_detection", kwargs={"pk": scan.pk}),
                data=json.dumps({"detection_id": det.pk}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["updated"], 1)

        det.refresh_from_db()
        self.assertEqual(det.confidence, 1.0)

        det_path = pathlib.Path(scan.output_dir) / "detections.json"
        self.assertTrue(det_path.exists())
        saved = json.loads(det_path.read_text())
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["confidence"], 1.0)

    def test_unknown_id_returns_404(self):
        """POST with a non-existent detection_id returns 404."""
        from unittest.mock import patch

        user = self.make_staff_user()
        self.client.force_login(user)
        scan, _det = self._make_scan_with_detection()

        with patch("scanning.s3_sync.upload_file_to_s3"):
            response = self.client.post(
                reverse("approve_detection", kwargs={"pk": scan.pk}),
                data=json.dumps({"detection_id": 999999}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 404)
        body = json.loads(response.content)
        self.assertEqual(body["status"], "error")
