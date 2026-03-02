import io
import shutil
import tempfile

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import models
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from scanning.factories import (
    OpinionScanFactory,
    ReporterFactory,
    ScanFactory,
    UserFactory,
)
from scanning.models import (
    OpinionScan,
    OpinionStatus,
    Scan,
    Source,
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


class TestScanUpload(ScanningTestCase):
    """Test the scan upload functionality."""

    def test_upload_page_renders(self):
        self.client.force_login(self.make_user())
        response = self.client.get(reverse("scan_upload"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Upload Book Scan")

    def test_successful_upload(self):
        user = self.make_user()
        self.client.force_login(user)
        reporter = ReporterFactory(
            short_name="a", full_name="Atlantic Reporter"
        )
        response = self.client.post(
            reverse("scan_upload"),
            {
                "reporter": reporter.pk,
                "source": Source.FULL,
                "volume": 1,
                "number_of_pages": 50,
                "start_page": 1,
                "end_page": 50,
                "original_pdf": self.make_pdf(),
            },
        )
        self.assertEqual(response.status_code, 302)
        scan = Scan.objects.get()
        self.assertEqual(scan.uploaded_by, user)
        self.assertEqual(scan.status, Status.UPLOADED)
        self.assertEqual(scan.reporter, reporter)

    def test_upload_missing_pdf(self):
        self.client.force_login(self.make_user())
        reporter = ReporterFactory()
        response = self.client.post(
            reverse("scan_upload"),
            {
                "reporter": reporter.pk,
                "source": Source.FULL,
                "volume": 1,
                "number_of_pages": 50,
                "start_page": 1,
                "end_page": 50,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Scan.objects.count(), 0)

    def test_upload_sets_uploaded_by(self):
        user = self.make_user()
        self.client.force_login(user)
        reporter = ReporterFactory(
            short_name="f3d", full_name="Federal Reporter, 3d"
        )
        self.client.post(
            reverse("scan_upload"),
            {
                "reporter": reporter.pk,
                "source": Source.FULL,
                "volume": 42,
                "number_of_pages": 200,
                "start_page": 1,
                "end_page": 200,
                "original_pdf": self.make_pdf(),
            },
        )
        scan = Scan.objects.get()
        self.assertEqual(scan.uploaded_by, user)
        self.assertEqual(scan.status, Status.UPLOADED)

    def test_upload_rejects_non_pdf_mime_type(self):
        self.client.force_login(self.make_user())
        reporter = ReporterFactory()
        fake_pdf = SimpleUploadedFile(
            "fake.pdf",
            b"not a pdf",
            content_type="text/plain",
        )
        response = self.client.post(
            reverse("scan_upload"),
            {
                "reporter": reporter.pk,
                "source": Source.FULL,
                "volume": 1,
                "number_of_pages": 50,
                "start_page": 1,
                "end_page": 50,
                "original_pdf": fake_pdf,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Scan.objects.count(), 0)


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

    def test_non_staff_no_review_form(self):
        user = self.make_user()
        self.client.force_login(user)
        scan = ScanFactory(uploaded_by=user)
        response = self.client.get(
            reverse("scan_detail", kwargs={"pk": scan.pk})
        )
        self.assertIsNone(response.context["review_form"])

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
        self.assertTrue(scan.original_pdf.name.startswith("books/a/"))
        self.assertTrue(scan.original_pdf.name.endswith(".pdf"))

    def test_book_upload_path_contains_pages(self):
        scan = ScanFactory(volume=3, start_page=10, end_page=200)
        self.assertIn("3_a_10-200_full.pdf", scan.original_pdf.name)

    def test_book_upload_path_opinions_source(self):
        scan = ScanFactory(
            volume=3, start_page=10, end_page=200, source=Source.OPINIONS
        )
        self.assertIn("3_a_10-200_opinions.pdf", scan.original_pdf.name)


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
        OpinionScanFactory.create_batch(30, uploaded_by=user)
        response = self.client.get(reverse("opinion_list"))
        self.assertEqual(len(response.context["page_obj"]), 25)
        response = self.client.get(reverse("opinion_list"), {"page": 2})
        self.assertEqual(len(response.context["page_obj"]), 5)


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
        self.assertContains(response, "Parent Book")
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


class TestOpinionReview(ScanningTestCase):
    """Test staff review functionality for opinion scans."""

    def test_staff_sees_review_form(self):
        staff = self.make_staff_user()
        self.client.force_login(staff)
        opinion = OpinionScanFactory()
        response = self.client.get(
            reverse("opinion_detail", kwargs={"pk": opinion.pk})
        )
        self.assertIsNotNone(response.context["review_form"])

    def test_non_staff_no_review_form(self):
        user = self.make_user()
        self.client.force_login(user)
        opinion = OpinionScanFactory(uploaded_by=user)
        response = self.client.get(
            reverse("opinion_detail", kwargs={"pk": opinion.pk})
        )
        self.assertIsNone(response.context["review_form"])

    def test_approve_sets_ok_status(self):
        staff = self.make_staff_user()
        self.client.force_login(staff)
        opinion = OpinionScanFactory()
        response = self.client.post(
            reverse("opinion_detail", kwargs={"pk": opinion.pk}),
            {"status": OpinionStatus.OK, "notes": "Looks good"},
        )
        self.assertEqual(response.status_code, 302)
        opinion.refresh_from_db()
        self.assertEqual(opinion.status, OpinionStatus.OK)
        self.assertEqual(opinion.notes, "Looks good")

    def test_reject_resets_to_no_status(self):
        staff = self.make_staff_user()
        self.client.force_login(staff)
        opinion = OpinionScanFactory(status=OpinionStatus.GAP)
        response = self.client.post(
            reverse("opinion_detail", kwargs={"pk": opinion.pk}),
            {"status": OpinionStatus.NO_STATUS, "notes": "Needs rescan"},
        )
        self.assertEqual(response.status_code, 302)
        opinion.refresh_from_db()
        self.assertEqual(opinion.status, OpinionStatus.NO_STATUS)
        self.assertEqual(opinion.notes, "Needs rescan")

    def test_approved_opinion_hides_review_form(self):
        staff = self.make_staff_user()
        self.client.force_login(staff)
        opinion = OpinionScanFactory(status=OpinionStatus.OK)
        response = self.client.get(
            reverse("opinion_detail", kwargs={"pk": opinion.pk})
        )
        self.assertIsNone(response.context["review_form"])
        self.assertContains(response, "Review Decision")
        self.assertContains(response, "OK")


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

    def test_page_start_less_than_page_end_ok(self):
        opinion = OpinionScanFactory(page_start=1, page_end=10)
        opinion.full_clean()  # should not raise

