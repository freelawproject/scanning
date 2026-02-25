import io
import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from scanning.factories import ReporterFactory, ScanFactory, UserFactory
from scanning.models import Scan, Status

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
    def make_pdf():
        """Create a minimal PDF file for upload testing.

        :returns: A SimpleUploadedFile containing PDF data.
        :rtype: SimpleUploadedFile
        """
        return SimpleUploadedFile(
            "test.pdf",
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
    """Test that authentication is required for protected views."""

    def test_scan_list_requires_login(self):
        response = self.client.get(reverse("scan_list"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    def test_upload_requires_login(self):
        response = self.client.get(reverse("scan_upload"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

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
        self.assertContains(response, "Upload Scan")

    def test_successful_upload(self):
        user = self.make_user()
        self.client.force_login(user)
        reporter = ReporterFactory(short_name="us", full_name="U.S. Reports")
        response = self.client.post(
            reverse("scan_upload"),
            {
                "reporter": reporter.pk,
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
        reporter_us = ReporterFactory(
            short_name="us", full_name="U.S. Reports"
        )
        reporter_f3d = ReporterFactory(
            short_name="f3d", full_name="Federal Reporter, 3d"
        )
        ScanFactory(uploaded_by=user, reporter=reporter_us)
        ScanFactory(uploaded_by=user, reporter=reporter_f3d)
        response = self.client.get(
            reverse("scan_list"), {"reporter": reporter_us.pk}
        )
        self.assertEqual(len(response.context["page_obj"]), 1)

    def test_pagination(self):
        user = self.make_user()
        self.client.force_login(user)
        ScanFactory.create_batch(30, uploaded_by=user)
        response = self.client.get(reverse("scan_list"))
        self.assertEqual(len(response.context["page_obj"]), 25)
        response = self.client.get(
            reverse("scan_list"), {"page": 2}
        )
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

    def test_404_for_nonexistent_scan(self):
        self.client.force_login(self.make_user())
        response = self.client.get(
            reverse("scan_detail", kwargs={"pk": 99999})
        )
        self.assertEqual(response.status_code, 404)


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
        scan = ScanFactory(volume=5)
        self.assertTrue(
            scan.original_pdf.name.startswith("uploads/us/5/")
        )
        self.assertTrue(scan.original_pdf.name.endswith(".pdf"))
