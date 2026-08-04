import io
import json
import os
import pathlib
import re
import shutil
import tempfile
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import models
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from scanning.factories import (
    OpinionScanFactory,
    ReporterFactory,
    ScanFactory,
    UserFactory,
    VolumeFactory,
)
from scanning.models import (
    Detection,
    OpinionScan,
    OpinionStatus,
    PageDeletion,
    PendingUpload,
    QueuedAction,
    QueueStatus,
    Scan,
    Source,
    Stage,
    Status,
    Volume,
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
    """The legacy scan detail URL redirects to the process view."""

    def test_redirects_to_process(self):
        user = self.make_user()
        self.client.force_login(user)
        scan = ScanFactory(uploaded_by=user)
        response = self.client.get(
            reverse("scan_detail", kwargs={"pk": scan.pk})
        )
        self.assertRedirects(
            response,
            reverse("scan_process", kwargs={"pk": scan.pk}),
            fetch_redirect_response=False,
        )

    def test_redirects_for_other_users_scan(self):
        user = self.make_user()
        self.client.force_login(user)
        scan = ScanFactory()  # different user
        response = self.client.get(
            reverse("scan_detail", kwargs={"pk": scan.pk})
        )
        self.assertRedirects(
            response,
            reverse("scan_process", kwargs={"pk": scan.pk}),
            fetch_redirect_response=False,
        )


class TestVolumeQueueStatusIntegration(ScanningTestCase):
    """Volume.queue_status updates when scans change state."""

    def _make_scan(self, volume, start=1, end=100, status=Status.UPLOADED):
        return ScanFactory(
            volume_obj=volume,
            reporter=volume.reporter,
            volume=volume.volume_number,
            start_page=start,
            end_page=end,
            number_of_pages=end - start + 1,
            status=status,
        )

    def test_approval_bumps_volume_to_complete(self):
        staff = self.make_staff_user()
        self.client.force_login(staff)
        volume = VolumeFactory(queue_status=QueueStatus.SCANNED)
        scan = self._make_scan(volume, status=Status.PENDING_REVIEW)
        scan.stage = Stage.APPROVED
        scan.save(update_fields=["stage"])
        response = self.client.post(
            reverse("approve_scan", kwargs={"pk": scan.pk})
        )
        self.assertEqual(response.status_code, 302)
        volume.refresh_from_db()
        self.assertEqual(volume.queue_status, QueueStatus.COMPLETE)

    def test_rejection_moves_volume_back_to_scanning(self):
        from scanning.services import refresh_volume_queue_status_for_scan

        volume = VolumeFactory(queue_status=QueueStatus.SCANNED)
        scan = self._make_scan(volume, end=50, status=Status.PENDING_REVIEW)
        scan.status = Status.UPLOADED
        scan.save(update_fields=["status"])
        refresh_volume_queue_status_for_scan(scan)
        volume.refresh_from_db()
        self.assertEqual(volume.queue_status, QueueStatus.SCANNING)

    def test_unavailable_volume_is_not_recomputed(self):
        staff = self.make_staff_user()
        self.client.force_login(staff)
        volume = VolumeFactory(queue_status=QueueStatus.UNAVAILABLE)
        scan = self._make_scan(volume, status=Status.PENDING_REVIEW)
        scan.stage = Stage.APPROVED
        scan.save(update_fields=["stage"])
        self.client.post(reverse("approve_scan", kwargs={"pk": scan.pk}))
        volume.refresh_from_db()
        self.assertEqual(volume.queue_status, QueueStatus.UNAVAILABLE)

    def test_admin_delete_refreshes_volume_queue_status(self):
        """Deleting a Scan via admin should refresh its parent Volume."""
        from django.contrib.admin.sites import AdminSite

        from scanning.admin import ScanAdmin

        volume = VolumeFactory(queue_status=QueueStatus.COMPLETE)
        scan = self._make_scan(volume, status=Status.APPROVED)
        admin_instance = ScanAdmin(Scan, AdminSite())
        request = RequestFactory().post("/admin/scanning/scan/")
        request.user = self.make_staff_user(is_superuser=True)
        admin_instance.delete_model(request=request, obj=scan)
        volume.refresh_from_db()
        self.assertEqual(volume.queue_status, QueueStatus.NEEDS_SCANNING)
        self.assertFalse(Scan.objects.filter(pk=scan.pk).exists())

    def test_admin_bulk_delete_refreshes_volumes(self):
        """Bulk-deleting Scans via admin should refresh each parent Volume."""
        from django.contrib.admin.sites import AdminSite

        from scanning.admin import ScanAdmin

        volume = VolumeFactory(queue_status=QueueStatus.COMPLETE)
        self._make_scan(volume, status=Status.APPROVED)
        admin_instance = ScanAdmin(Scan, AdminSite())
        request = RequestFactory().post("/admin/scanning/scan/")
        request.user = self.make_staff_user(is_superuser=True)
        admin_instance.delete_queryset(
            request=request, queryset=Scan.objects.filter(volume_obj=volume)
        )
        volume.refresh_from_db()
        self.assertEqual(volume.queue_status, QueueStatus.NEEDS_SCANNING)
        self.assertEqual(Scan.objects.filter(volume_obj=volume).count(), 0)

    @override_settings(DEVELOPMENT=True)
    def test_upload_bumps_assigned_to_scanning(self):
        user = self.make_user()
        self.client.force_login(user)
        volume = VolumeFactory(
            queue_status=QueueStatus.ASSIGNED,
            assigned_to=user,
            assigned_at=timezone.now(),
        )
        pdf = SimpleUploadedFile(
            "scan.pdf", b"%PDF-1.4 body", content_type="application/pdf"
        )
        response = self.client.post(
            reverse(
                "queue_upload",
                kwargs={
                    "reporter_slug": volume.reporter.short_name,
                    "vol": volume.volume_number,
                },
            ),
            {
                "new_scan": "1",
                "first_page": "1",
                "last_page": "50",
                "original_pdf": pdf,
            },
        )
        self.assertEqual(response.status_code, 302)
        volume.refresh_from_db()
        self.assertEqual(volume.queue_status, QueueStatus.SCANNING)


class TestQueueUploadXhr(ScanningTestCase):
    """XHR uploads get a JSON redirect instead of a 302."""

    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.volume = VolumeFactory()
        self.url = reverse(
            "queue_upload",
            kwargs={
                "reporter_slug": self.volume.reporter.short_name,
                "vol": self.volume.volume_number,
            },
        )
        self.queue_url = reverse(
            "queue_detail",
            kwargs={
                "reporter_slug": self.volume.reporter.short_name,
                "vol": self.volume.volume_number,
            },
        )

    @override_settings(DEVELOPMENT=True)
    def test_xhr_upload_returns_json_redirect(self):
        pdf = SimpleUploadedFile(
            "scan.pdf", b"%PDF-1.4 body", content_type="application/pdf"
        )
        response = self.client.post(
            self.url,
            {"new_scan": "1", "original_pdf": pdf},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["redirect"], self.queue_url)

    @override_settings(DEVELOPMENT=True)
    def test_xhr_upload_validate_redirects_to_process(self):
        pdf = SimpleUploadedFile(
            "scan.pdf", b"%PDF-1.4 body", content_type="application/pdf"
        )
        response = self.client.post(
            self.url,
            {
                "new_scan": "1",
                "original_pdf": pdf,
                "action": "upload_validate",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        scan = Scan.objects.get(volume_obj=self.volume)
        self.assertEqual(
            response.json()["redirect"],
            reverse("scan_process", kwargs={"pk": scan.pk}),
        )

    def test_xhr_invalid_pdf_returns_json_redirect(self):
        fake = SimpleUploadedFile(
            "scan.pdf", b"not a pdf", content_type="application/pdf"
        )
        response = self.client.post(
            self.url,
            {"new_scan": "1", "original_pdf": fake},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["redirect"], self.queue_url)
        self.assertFalse(Scan.objects.filter(volume_obj=self.volume).exists())

    @override_settings(DEVELOPMENT=False)
    def test_xhr_prod_upload_returns_json_redirect(self):
        pdf = SimpleUploadedFile(
            "scan.pdf", b"%PDF-1.4 body", content_type="application/pdf"
        )
        with (
            patch("scanning.views.has_s3_credentials", return_value=True),
            patch(
                "scanning.s3_sync.upload_fileobj_to_s3", return_value=True
            ) as mock_upload,
        ):
            response = self.client.post(
                self.url,
                {"new_scan": "1", "original_pdf": pdf},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["redirect"], self.queue_url)
        mock_upload.assert_called_once()
        scan = Scan.objects.get(volume_obj=self.volume)
        self.assertTrue(scan.original_pdf.name)
        # The prod path streams straight to S3; it must not write a
        # local copy of the original PDF (issue #94).
        out_dir = pathlib.Path(scan.output_dir)
        self.assertFalse(
            out_dir.exists() and list(out_dir.glob("*.original.pdf")),
            "prod upload must not write a local original PDF",
        )

    @override_settings(DEVELOPMENT=False)
    def test_xhr_prod_upload_without_credentials_returns_json_redirect(self):
        pdf = SimpleUploadedFile(
            "scan.pdf", b"%PDF-1.4 body", content_type="application/pdf"
        )
        with patch("scanning.views.has_s3_credentials", return_value=False):
            response = self.client.post(
                self.url,
                {"new_scan": "1", "original_pdf": pdf},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["redirect"], self.queue_url)
        self.assertFalse(Scan.objects.filter(volume_obj=self.volume).exists())


class TestPresignedUpload(ScanningTestCase):
    """Presigned direct-to-S3 upload: presign + confirm endpoints."""

    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.volume = VolumeFactory()
        kwargs = {
            "reporter_slug": self.volume.reporter.short_name,
            "vol": self.volume.volume_number,
        }
        self.presign_url = reverse("presign_scan_upload", kwargs=kwargs)
        self.confirm_url = reverse("confirm_scan_upload", kwargs=kwargs)
        self.queue_url = reverse("queue_detail", kwargs=kwargs)

    def _presign(self, **extra):
        """Hit the presign endpoint with S3 stubbed; return (response, mock)."""
        data = {
            "new_scan": "1",
            "filename": "scan.pdf",
            "content_type": "application/pdf",
            "size": "1024",
        }
        data.update(extra)
        fake = {"url": "https://s3.example/bucket", "fields": {"key": "k"}}
        with (
            patch("scanning.views.has_s3_credentials", return_value=True),
            patch(
                "scanning.s3_sync.generate_presigned_post", return_value=fake
            ) as mock_presign,
        ):
            response = self.client.post(
                self.presign_url,
                data,
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
        return response, mock_presign

    def test_presign_creates_scan_and_pending(self):
        response, mock_presign = self._presign()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("presigned", body)
        scan = Scan.objects.get(volume_obj=self.volume)
        self.assertEqual(scan.status, Status.UPLOADED)
        # The original bytes must NOT flow through Django here.
        self.assertFalse(scan.original_pdf.name)
        pending = PendingUpload.objects.get(pk=body["pending_id"])
        self.assertEqual(pending.scan_id, scan.pk)
        self.assertTrue(pending.s3_key.endswith(".original.pdf"))
        mock_presign.assert_called_once()

    def test_presign_rejects_non_pdf(self):
        response, _ = self._presign(filename="notes.txt")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Scan.objects.filter(volume_obj=self.volume).exists())
        self.assertFalse(PendingUpload.objects.exists())

    def test_presign_rejects_oversized(self):
        response, _ = self._presign(size=str(4 * 1024**3))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Scan.objects.filter(volume_obj=self.volume).exists())

    def test_presign_without_credentials(self):
        with patch("scanning.views.has_s3_credentials", return_value=False):
            response = self.client.post(
                self.presign_url,
                {
                    "new_scan": "1",
                    "filename": "scan.pdf",
                    "content_type": "application/pdf",
                    "size": "1024",
                },
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
        self.assertEqual(response.status_code, 503)
        self.assertFalse(Scan.objects.filter(volume_obj=self.volume).exists())

    def test_confirm_attaches_and_queues(self):
        pending_id = self._presign()[0].json()["pending_id"]
        with patch(
            "scanning.s3_sync.verify_uploaded_object", return_value=True
        ):
            response = self.client.post(
                self.confirm_url,
                {"pending_id": pending_id, "action": "upload_validate"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
        self.assertEqual(response.status_code, 200)
        scan = Scan.objects.get(volume_obj=self.volume)
        self.assertTrue(scan.original_pdf.name)
        self.assertEqual(scan.status, Status.QUEUED)
        self.assertEqual(scan.queued_action, QueuedAction.FULL_PIPELINE)
        self.assertEqual(
            response.json()["redirect"],
            reverse("scan_process", kwargs={"pk": scan.pk}),
        )
        self.assertFalse(PendingUpload.objects.filter(pk=pending_id).exists())

    def test_confirm_upload_only_stays_uploaded(self):
        pending_id = self._presign()[0].json()["pending_id"]
        with patch(
            "scanning.s3_sync.verify_uploaded_object", return_value=True
        ):
            response = self.client.post(
                self.confirm_url,
                {"pending_id": pending_id, "action": "upload_only"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["redirect"], self.queue_url)
        scan = Scan.objects.get(volume_obj=self.volume)
        self.assertEqual(scan.status, Status.UPLOADED)

    def test_confirm_failure_deletes_orphan_scan(self):
        pending_id = self._presign()[0].json()["pending_id"]
        with patch(
            "scanning.s3_sync.verify_uploaded_object", return_value=False
        ):
            response = self.client.post(
                self.confirm_url,
                {"pending_id": pending_id, "action": "upload_validate"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
        self.assertEqual(response.status_code, 400)
        # Fresh, fileless scan gets cleaned up along with the pending row.
        self.assertFalse(Scan.objects.filter(volume_obj=self.volume).exists())
        self.assertFalse(PendingUpload.objects.filter(pk=pending_id).exists())

    def test_confirm_failure_keeps_scan_that_has_a_file(self):
        # Re-upload of an existing scan: a failed verification must not
        # delete the scan (it still has its previous original PDF).
        scan = ScanFactory(
            volume_obj=self.volume,
            reporter=self.volume.reporter,
            volume=self.volume.volume_number,
        )
        self.assertTrue(scan.original_pdf.name)
        pending = PendingUpload.objects.create(
            scan=scan,
            s3_key="processing/x/x.original.pdf",
            expected_size=1024,
            created_by=self.user,
        )
        with patch(
            "scanning.s3_sync.verify_uploaded_object", return_value=False
        ):
            response = self.client.post(
                self.confirm_url,
                {"pending_id": str(pending.id), "action": "upload_only"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(Scan.objects.filter(pk=scan.pk).exists())
        self.assertFalse(PendingUpload.objects.filter(pk=pending.id).exists())

    def test_confirm_failure_reupload_keeps_s3_object(self):
        # A re-upload's s3_key is the confirmed original's key, so a failed
        # verification must not delete the object out from under the scan.
        scan = ScanFactory(
            volume_obj=self.volume,
            reporter=self.volume.reporter,
            volume=self.volume.volume_number,
        )
        self.assertTrue(scan.original_pdf.name)
        pending = PendingUpload.objects.create(
            scan=scan,
            s3_key="processing/x/x.original.pdf",
            expected_size=1024,
            created_by=self.user,
        )
        with (
            patch(
                "scanning.s3_sync.verify_uploaded_object", return_value=False
            ),
            patch("scanning.s3_sync.delete_uploaded_object") as mock_delete,
        ):
            response = self.client.post(
                self.confirm_url,
                {"pending_id": str(pending.id), "action": "upload_only"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
        self.assertEqual(response.status_code, 400)
        mock_delete.assert_not_called()
        self.assertTrue(Scan.objects.filter(pk=scan.pk).exists())

    def test_confirm_rejects_other_users_pending(self):
        pending_id = self._presign()[0].json()["pending_id"]
        other = self.make_user(username="intruder")
        self.client.force_login(other)
        response = self.client.post(
            self.confirm_url,
            {"pending_id": pending_id, "action": "upload_only"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 404)

    def test_confirm_rejects_pending_from_other_volume(self):
        # A pending whose scan lives in a different volume can't be
        # confirmed through this volume's endpoint, even by its owner.
        other_volume = VolumeFactory()
        scan = ScanFactory(
            volume_obj=other_volume,
            reporter=other_volume.reporter,
            volume=other_volume.volume_number,
        )
        pending = PendingUpload.objects.create(
            scan=scan,
            s3_key="processing/x/x.original.pdf",
            expected_size=1024,
            created_by=self.user,
        )
        response = self.client.post(
            self.confirm_url,  # built from self.volume, not other_volume
            {"pending_id": str(pending.id), "action": "upload_only"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 404)

    def test_presign_cleans_up_scan_on_error(self):
        # A boto/network error during presign must not leave an orphaned,
        # fileless scan: with no PendingUpload row the TTL sweep can't
        # reclaim it, so the view deletes it inline.
        data = {
            "new_scan": "1",
            "filename": "scan.pdf",
            "content_type": "application/pdf",
            "size": "1024",
        }
        with (
            patch("scanning.views.has_s3_credentials", return_value=True),
            patch(
                "scanning.s3_sync.generate_presigned_post",
                side_effect=Exception("boom"),
            ),
        ):
            response = self.client.post(
                self.presign_url,
                data,
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
        self.assertEqual(response.status_code, 503)
        self.assertFalse(Scan.objects.filter(volume_obj=self.volume).exists())
        self.assertFalse(PendingUpload.objects.exists())

    def test_presign_cleans_up_when_pending_create_fails(self):
        # If the PendingUpload insert fails after a successful presign, the
        # fileless scan must still be cleaned up (no pending row => the TTL
        # sweep can't find it).
        data = {
            "new_scan": "1",
            "filename": "scan.pdf",
            "content_type": "application/pdf",
            "size": "1024",
        }
        fake = {"url": "https://s3.example/bucket", "fields": {"key": "k"}}
        with (
            patch("scanning.views.has_s3_credentials", return_value=True),
            patch(
                "scanning.s3_sync.generate_presigned_post", return_value=fake
            ),
            patch(
                "scanning.views.PendingUpload.objects.create",
                side_effect=Exception("db blip"),
            ),
        ):
            response = self.client.post(
                self.presign_url,
                data,
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
        self.assertEqual(response.status_code, 503)
        self.assertFalse(Scan.objects.filter(volume_obj=self.volume).exists())
        self.assertFalse(PendingUpload.objects.exists())

    def test_presign_pins_pdf_content_type(self):
        # The stored Content-Type must be pinned to application/pdf, never
        # the browser-supplied value.
        _, mock_presign = self._presign(content_type="text/html")
        self.assertEqual(mock_presign.call_args.args[2], "application/pdf")

    def test_presign_stores_action(self):
        # The chosen action is persisted so recovery can replay it.
        resp, _ = self._presign(action="upload_validate")
        pending = PendingUpload.objects.get(pk=resp.json()["pending_id"])
        self.assertEqual(pending.action, "upload_validate")

    def test_presign_defaults_action_to_upload_only(self):
        resp, _ = self._presign()
        pending = PendingUpload.objects.get(pk=resp.json()["pending_id"])
        self.assertEqual(pending.action, "upload_only")

    def test_presign_rejects_reupload_of_scan_with_file(self):
        # A scan_pk upload onto a scan that already has a confirmed original
        # would overwrite it in S3 (same deterministic key). Refuse it.
        scan = ScanFactory(
            volume_obj=self.volume,
            reporter=self.volume.reporter,
            volume=self.volume.volume_number,
        )
        self.assertTrue(scan.original_pdf.name)
        with patch("scanning.views.has_s3_credentials", return_value=True):
            response = self.client.post(
                self.presign_url,
                {"scan_pk": str(scan.pk), "filename": "x.pdf", "size": "1024"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(PendingUpload.objects.exists())

    def test_confirm_rejects_invalid_pending_id(self):
        # A non-UUID pending_id must return a clean 400, not a 500.
        response = self.client.post(
            self.confirm_url,
            {"pending_id": "not-a-uuid", "action": "upload_only"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 400)

    def test_confirm_failure_deletes_s3_object(self):
        pending_id = self._presign()[0].json()["pending_id"]
        pending = PendingUpload.objects.get(pk=pending_id)
        with (
            patch(
                "scanning.s3_sync.verify_uploaded_object", return_value=False
            ),
            patch("scanning.s3_sync.delete_uploaded_object") as mock_delete,
        ):
            response = self.client.post(
                self.confirm_url,
                {"pending_id": pending_id, "action": "upload_validate"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
        self.assertEqual(response.status_code, 400)
        mock_delete.assert_called_once_with(pending.s3_key)


class TestServeOriginalCrop(ScanningTestCase):
    """serve_original_crop lazily pulls the original PDF from S3."""

    def _crop_url(self, pk):
        return (
            reverse("serve_original_crop", kwargs={"pk": pk})
            + "?page=0&x0=0&y0=0&x1=100&y1=100&dpi=72"
        )

    def test_lazy_pulls_original_when_missing(self):
        user = self.make_user()
        self.client.force_login(user)

        tmp_root = tempfile.mkdtemp()
        reporter = ReporterFactory(short_name="oc2d")
        scan = ScanFactory(
            reporter=reporter, volume=5, start_page=1, end_page=2
        )
        # No local original resolvable (prod: it lives only in S3).
        original_path = scan.original_pdf.path
        if os.path.exists(original_path):
            os.remove(original_path)

        fixture = (
            pathlib.Path(__file__).resolve().parent
            / "fixtures"
            / "a3d.332.1.1.pdf"
        )

        def _fake_pull(scan_arg):
            output = pathlib.Path(scan_arg.output_dir)
            output.mkdir(parents=True, exist_ok=True)
            name = pathlib.Path(scan_arg.original_pdf.name).name
            (output / name).write_bytes(fixture.read_bytes())

        with (
            override_settings(
                DEVELOPMENT=False,
                TESTING=False,
                PROCESSING_TMP_DIR=tmp_root,
            ),
            patch(
                "scanning.s3_sync.download_original_pdf",
                side_effect=_fake_pull,
            ) as mock_pull,
        ):
            response = self.client.get(self._crop_url(scan.pk))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        mock_pull.assert_called_once()

    def test_returns_404_when_original_unavailable(self):
        user = self.make_user()
        self.client.force_login(user)

        tmp_root = tempfile.mkdtemp()
        scan = ScanFactory(start_page=1, end_page=2)
        original_path = scan.original_pdf.path
        if os.path.exists(original_path):
            os.remove(original_path)

        with (
            override_settings(
                DEVELOPMENT=False,
                TESTING=False,
                PROCESSING_TMP_DIR=tmp_root,
            ),
            patch("scanning.s3_sync.download_original_pdf"),
        ):
            response = self.client.get(self._crop_url(scan.pk))

        self.assertEqual(response.status_code, 404)


class TestQueueView(ScanningTestCase):
    """The queue page renders and paginates volumes."""

    def test_renders_with_page_obj(self):
        user = self.make_user()
        self.client.force_login(user)
        VolumeFactory()
        response = self.client.get(reverse("queue"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("page_obj", response.context)

    def test_paginates_at_100_per_page(self):
        user = self.make_user()
        self.client.force_login(user)
        VolumeFactory.create_batch(105)
        first = self.client.get(reverse("queue"))
        self.assertEqual(len(first.context["page_obj"]), 100)
        self.assertEqual(first.context["page_obj"].paginator.num_pages, 2)
        second = self.client.get(reverse("queue"), {"page": 2})
        self.assertEqual(len(second.context["page_obj"]), 5)

    def test_stats_count_all_volumes_not_just_page(self):
        user = self.make_user()
        self.client.force_login(user)
        VolumeFactory.create_batch(55, queue_status=QueueStatus.NEEDS_SCANNING)
        response = self.client.get(reverse("queue"))
        self.assertEqual(response.context["stats"]["total"], 55)
        self.assertEqual(response.context["stats"]["needs_scanning"], 55)


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
            response, reverse("scan_process", kwargs={"pk": scan.pk})
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
        redacted_dir.mkdir()
        (redacted_dir / "a.1.0001-0010.pdf").write_bytes(b"%PDF-1.4")
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

    def test_approve_with_files_flips_status(self):
        """Approving a generated scan flips status to APPROVED.

        Approve is now a pure status flip — the generate step already
        pushed files to ``processing/<pk>/...`` on S3, so this view does
        not promote anything. Just confirms the human signed off.
        """
        user = self.make_staff_user()
        self.client.force_login(user)
        scan = self._make_scan_with_generated_files()

        self.client.post(
            reverse("approve_scan", kwargs={"pk": scan.pk}),
            follow=True,
        )
        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.APPROVED)

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

        with patch("scanning.s3_sync.upload_fileobj_to_s3") as mock_s3:
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
                "scanning.s3_sync.upload_fileobj_to_s3", return_value=True
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
                "scanning.s3_sync.upload_fileobj_to_s3",
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
            patch("scanning.s3_sync.upload_fileobj_to_s3") as mock_s3,
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

        # upload_fileobj_to_s3 returning False (e.g. the helper's own
        # short-circuit) should be treated as a failure by the view.
        with (
            patch("scanning.views.has_s3_credentials", return_value=True),
            patch("scanning.s3_sync.upload_fileobj_to_s3", return_value=False),
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
    """serve_scan_pdf serves only the preview PDF, pulling it lazily."""

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
                "scanning.s3_sync.download_preview_pdf",
                side_effect=_fake_download,
            ) as mock_pull,
        ):
            response = self.client.get(
                reverse("serve_scan_pdf", kwargs={"pk": scan.pk})
            )

        self.assertEqual(response.status_code, 200)
        mock_pull.assert_called_once()

    def test_serves_bitonal_even_with_local_original(self):
        """A local original must not pre-empt the pulled bitonal preview."""
        user = self.make_user()
        self.client.force_login(user)

        tmp_root = tempfile.mkdtemp()
        reporter = ReporterFactory(short_name="bt2d")
        scan = ScanFactory(
            reporter=reporter, volume=12, start_page=1, end_page=2
        )
        # The original is resolvable locally via the FileField.
        self.assertTrue(os.path.exists(scan.original_pdf.path))

        bitonal_bytes = b"%PDF-1.4 pulled bitonal"

        def _fake_download(scan_arg):
            output = pathlib.Path(scan_arg.output_dir)
            output.mkdir(parents=True, exist_ok=True)
            (output / "bitonal.pdf").write_bytes(bitonal_bytes)

        with (
            override_settings(
                DEVELOPMENT=False,
                TESTING=False,
                PROCESSING_TMP_DIR=tmp_root,
            ),
            patch(
                "scanning.s3_sync.download_preview_pdf",
                side_effect=_fake_download,
            ) as mock_pull,
        ):
            response = self.client.get(
                reverse("serve_scan_pdf", kwargs={"pk": scan.pk})
            )

        self.assertEqual(response.status_code, 200)
        mock_pull.assert_called_once()
        # The bitonal was served, not the (different) original.
        served = b"".join(response.streaming_content)
        self.assertEqual(served, bitonal_bytes)

    def test_not_ready_returns_202_when_no_preview(self):
        """With no preview anywhere, return 202 'still processing'."""
        user = self.make_user()
        self.client.force_login(user)

        tmp_root = tempfile.mkdtemp()
        scan = ScanFactory(start_page=1, end_page=2, status=Status.PROCESSING)

        with (
            override_settings(
                DEVELOPMENT=False,
                TESTING=False,
                PROCESSING_TMP_DIR=tmp_root,
            ),
            patch("scanning.s3_sync.download_preview_pdf"),
        ):
            response = self.client.get(
                reverse("serve_scan_pdf", kwargs={"pk": scan.pk})
            )

        self.assertEqual(response.status_code, 202)
        data = response.json()
        self.assertEqual(data["status"], "not_ready")
        self.assertEqual(data["scan_status"], Status.PROCESSING)
        self.assertIn("processing", data["message"].lower())

    def test_original_is_never_served(self):
        """Even with a local original and no preview, never stream it."""
        user = self.make_user()
        self.client.force_login(user)

        tmp_root = tempfile.mkdtemp()
        scan = ScanFactory(start_page=1, end_page=2, status=Status.QUEUED)
        # The original PDF is resolvable locally; the endpoint must still
        # refuse to serve it (it's the multi-GB file we never stream).
        self.assertTrue(os.path.exists(scan.original_pdf.path))

        with (
            override_settings(
                DEVELOPMENT=False,
                TESTING=False,
                PROCESSING_TMP_DIR=tmp_root,
            ),
            patch("scanning.s3_sync.download_preview_pdf"),
        ):
            response = self.client.get(
                reverse("serve_scan_pdf", kwargs={"pk": scan.pk})
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "not_ready")

    def test_terminal_status_returns_409_not_202(self):
        """An errored scan is terminal: 409 so the viewer stops polling."""
        user = self.make_user()
        self.client.force_login(user)

        tmp_root = tempfile.mkdtemp()
        scan = ScanFactory(start_page=1, end_page=2, status=Status.ERROR)

        with (
            override_settings(
                DEVELOPMENT=False,
                TESTING=False,
                PROCESSING_TMP_DIR=tmp_root,
            ),
            patch("scanning.s3_sync.download_preview_pdf"),
        ):
            response = self.client.get(
                reverse("serve_scan_pdf", kwargs={"pk": scan.pk})
            )

        self.assertEqual(response.status_code, 409)
        data = response.json()
        self.assertEqual(data["status"], "unavailable")
        self.assertEqual(data["scan_status"], Status.ERROR)


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


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestPendingChangesGuard(ScanningTestCase):
    """The validate/detect/recheck actions are blocked while page
    changes are pending, so a deletion can't be silently stranded
    behind an action that ignores it (and a full re-validation can't
    burn a RunPod run for nothing)."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.scan = ScanFactory(
            uploaded_by=self.user, status=Status.PENDING_REVIEW
        )
        PageDeletion.objects.create(scan=self.scan, pdf_page=2)

    def _assert_blocked(self, url_name):
        response = self.client.post(
            reverse(url_name, kwargs={"pk": self.scan.pk})
        )
        self.assertRedirects(
            response,
            reverse("scan_process", kwargs={"pk": self.scan.pk}) + "?step=1",
            fetch_redirect_response=False,
        )
        self.scan.refresh_from_db()
        # The action must NOT have queued the scan.
        self.assertEqual(self.scan.status, Status.PENDING_REVIEW)
        # The pending deletion is untouched.
        self.assertTrue(self.scan.deletions.exists())

    def test_start_validate_blocked(self):
        """Re-validate is blocked while a deletion is pending."""
        self._assert_blocked("start_validate")

    def test_start_detect_blocked(self):
        """Next: Detect is blocked while a deletion is pending."""
        self._assert_blocked("start_detect")

    def test_recalculate_blocked(self):
        """Recheck is blocked while a deletion is pending."""
        self._assert_blocked("recalculate")

    def test_start_validate_allowed_without_pending(self):
        """With no pending changes, Re-validate queues the scan."""
        self.scan.deletions.all().delete()
        response = self.client.post(
            reverse("start_validate", kwargs={"pk": self.scan.pk})
        )
        self.assertEqual(response.status_code, 302)
        self.scan.refresh_from_db()
        self.assertEqual(self.scan.status, Status.QUEUED)
        self.assertEqual(self.scan.queued_action, QueuedAction.FULL_PIPELINE)


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestRecalculateView(ScanningTestCase):
    """Recheck works on a web pod that never pulled the scan's files from
    S3, so clicking it in step 1 does not 500 (SCANNING-1S)."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.scan = ScanFactory(
            uploaded_by=self.user,
            status=Status.PENDING_REVIEW,
            start_page=1,
            end_page=2,
            page_count=2,
            ocr_results=[
                {"pdf_page": 1, "detected": "1", "type": "single"},
                {"pdf_page": 2, "detected": "2", "type": "single"},
            ],
        )
        # Production keeps the PDF in S3 only; the request thread has no
        # local copy.
        pathlib.Path(self.scan.original_pdf.path).unlink()

    def test_recheck_without_local_pdf(self):
        """The Recheck POST redirects back to the viewer and rebuilds the
        page_map from stored OCR results."""
        response = self.client.post(
            reverse("recalculate", kwargs={"pk": self.scan.pk})
        )
        self.assertEqual(response.status_code, 302)
        self.scan.refresh_from_db()
        self.assertEqual(self.scan.page_count, 2)
        self.assertTrue(self.scan.page_map)

    def test_recheck_without_ocr_results_is_a_noop(self):
        """With nothing to recompute the view redirects without touching
        the scan."""
        self.scan.ocr_results = []
        self.scan.save(update_fields=["ocr_results"])
        response = self.client.post(
            reverse("recalculate", kwargs={"pk": self.scan.pk})
        )
        self.assertEqual(response.status_code, 302)
        self.scan.refresh_from_db()
        self.assertEqual(self.scan.page_map, [])


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestProcessActionsFragment(ScanningTestCase):
    """The process_actions endpoint renders the step action bar so the
    viewer can refresh it in place after a deletion/undo."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.scan = ScanFactory(
            uploaded_by=self.user, status=Status.PENDING_REVIEW, page_count=5
        )

    def test_no_pending_changes(self):
        """Without pending changes the bar has no Rebuild button."""
        response = self.client.get(
            reverse("process_actions", kwargs={"pk": self.scan.pk}) + "?step=1"
        )
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        self.assertFalse(body["has_pending_changes"])
        self.assertNotIn("Rebuild &amp; Validate", body["html"])

    def test_pending_deletion_shows_rebuild(self):
        """A pending deletion makes the bar show Rebuild & Validate."""
        PageDeletion.objects.create(scan=self.scan, pdf_page=2)
        response = self.client.get(
            reverse("process_actions", kwargs={"pk": self.scan.pk}) + "?step=1"
        )
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        self.assertTrue(body["has_pending_changes"])
        self.assertIn("Rebuild &amp; Validate", body["html"])
        self.assertIn(
            reverse("reprocess", kwargs={"pk": self.scan.pk}), body["html"]
        )


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestAssignPage(ScanningTestCase):
    """Manual page-number edits validate input, support clearing, and
    rebuild the page_map so duplicate flags stay in sync with the edit."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)
        self.scan = ScanFactory(
            uploaded_by=self.user,
            status=Status.PENDING_REVIEW,
            page_count=3,
            ocr_results=[
                {
                    "pdf_page": 1,
                    "detected": "5",
                    "type": "single",
                    "zone": "yolo-pn",
                },
                {"pdf_page": 2, "detected": None, "type": None, "zone": None},
                {
                    "pdf_page": 3,
                    "detected": "9",
                    "type": "single",
                    "zone": "yolo-pn",
                },
            ],
        )

    def _post(self, pdf_page, page_number):
        return self.client.post(
            reverse("assign_page", kwargs={"pk": self.scan.pk}),
            data=json.dumps(
                {"pdf_page": pdf_page, "page_number": page_number}
            ),
            content_type="application/json",
        )

    def test_rejects_zero(self):
        """0 is not a valid page number and leaves the value unchanged."""
        response = self._post(1, "0")
        self.assertEqual(response.status_code, 400)
        self.scan.refresh_from_db()
        self.assertEqual(self.scan.ocr_results[0]["detected"], "5")

    def test_rejects_negative(self):
        """Negative numbers are rejected."""
        self.assertEqual(self._post(1, "-3").status_code, 400)

    def test_rejects_non_numeric(self):
        """Non-numeric input is rejected."""
        self.assertEqual(self._post(1, "abc").status_code, 400)

    def test_unknown_page_returns_404(self):
        """Assigning to a PDF page not in ocr_results is a 404."""
        self.assertEqual(self._post(99, "5").status_code, 404)

    def test_missing_page_number_key_rejected(self):
        """Omitting page_number is a 400, not a silent clear (clearing is
        explicit via null/empty)."""
        response = self.client.post(
            reverse("assign_page", kwargs={"pk": self.scan.pk}),
            data=json.dumps({"pdf_page": 1}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.scan.refresh_from_db()
        self.assertEqual(self.scan.ocr_results[0]["detected"], "5")

    def test_missing_pdf_page_rejected(self):
        """Omitting pdf_page is a 400 rather than a server error."""
        response = self.client.post(
            reverse("assign_page", kwargs={"pk": self.scan.pk}),
            data=json.dumps({"page_number": "5"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_sets_valid_number(self):
        """A positive number is stored and marked manual."""
        response = self._post(3, "7")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["detected"], "7")
        self.scan.refresh_from_db()
        r = self.scan.ocr_results[2]
        self.assertEqual(r["detected"], "7")
        self.assertEqual(r["type"], "single")
        self.assertEqual(r["zone"], "manual")

    def test_blank_clears_number(self):
        """A blank value clears the page number (marks it unnumbered)."""
        response = self._post(1, "")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(json.loads(response.content)["detected"])
        self.scan.refresh_from_db()
        r = self.scan.ocr_results[0]
        self.assertIsNone(r["detected"])
        self.assertEqual(r["zone"], "manual")

    def test_null_clears_number(self):
        """A null value also clears the page number."""
        response = self._post(1, None)
        self.assertEqual(response.status_code, 200)
        self.scan.refresh_from_db()
        self.assertIsNone(self.scan.ocr_results[0]["detected"])

    def test_edit_creating_duplicate_flags_page_map(self):
        """Assigning a number that already exists flags every copy of that
        number as a duplicate in the rebuilt page_map (blackletter #55 flags
        all copies, not just the later one)."""
        response = self._post(3, "5")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(json.loads(response.content)["duplicate"])
        self.scan.refresh_from_db()
        flagged = {
            e["pdf_index"] for e in self.scan.page_map if e.get("duplicate")
        }
        self.assertIn(2, flagged)  # pdf_page 3 -> index 2 (newly-created "5")
        self.assertIn(0, flagged)  # the pre-existing "5" is flagged too

    def test_clearing_duplicate_unflags_page_map(self):
        """Clearing a duplicated number removes the duplicate flag."""
        self._post(3, "5")
        self.scan.refresh_from_db()
        self.assertTrue(any(e.get("duplicate") for e in self.scan.page_map))
        self._post(3, "")
        self.scan.refresh_from_db()
        flagged = {
            e["pdf_index"] for e in self.scan.page_map if e.get("duplicate")
        }
        self.assertNotIn(2, flagged)


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestSidebarDuplicateMarkers(ScanningTestCase):
    """The step-1 sidebar page list marks duplicates from the same
    page_map data the PDF viewer uses, so the two views agree even when
    the duplicate copies are not consecutive (the old consecutive-only
    check missed those, leaving the viewer and sidebar inconsistent)."""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)
        # Page 5's number repeats on pdf pages 1 and 3, separated by an
        # undetected page so the sequence check resets and never sees the
        # two "5"s as consecutive. Only the page_map carries the duplicate.
        self.scan = ScanFactory(
            uploaded_by=self.user,
            status=Status.PENDING_REVIEW,
            page_count=4,
            ocr_results=[
                {"pdf_page": 1, "detected": "5", "type": "single"},
                {"pdf_page": 2, "detected": None, "type": None},
                {"pdf_page": 3, "detected": "5", "type": "single"},
                {"pdf_page": 4, "detected": "6", "type": "single"},
            ],
            page_map=[
                {"type": "pdf_page", "pdf_index": 0, "logical_number": 5},
                {"type": "pdf_page", "pdf_index": 1, "logical_number": 2},
                {
                    "type": "pdf_page",
                    "pdf_index": 2,
                    "logical_number": 5,
                    "duplicate": True,
                },
                {"type": "pdf_page", "pdf_index": 3, "logical_number": 6},
            ],
        )

    def test_page_map_duplicate_is_marked_in_sidebar(self):
        """A page_map duplicate gets a DUP badge even when its copies are
        not consecutive in the numbering sequence."""
        response = self.client.get(
            reverse("scan_process", kwargs={"pk": self.scan.pk}) + "?step=1"
        )
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        # Exactly one DUP badge is rendered (the consecutive-check divider was
        # removed, so the badge is the only ">DUP<" left), matching the single
        # page_map duplicate, not zero (the old consecutive check) nor more.
        self.assertEqual(html.count(">DUP<"), 1)
        # The orange duplicate highlight must land on the right row. Each page
        # row carries its classes next to a unique ``data-pdf-index``; scope
        # the check to those rows so an unrelated bg-orange-100 elsewhere on
        # the page can neither satisfy nor mask the assertion. pdf page 3 is
        # pdf_index 2.
        row_classes = {
            int(idx): classes
            for classes, idx in re.findall(
                r'class="([^"]*)"\s+data-pdf-index="(\d+)"', html
            )
        }
        self.assertIn("bg-orange-100", row_classes[2])
        self.assertEqual(
            [
                idx
                for idx, cls in row_classes.items()
                if "bg-orange-100" in cls
            ],
            [2],
        )
