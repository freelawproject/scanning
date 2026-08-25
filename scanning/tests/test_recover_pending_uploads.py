"""Tests for recovering completed-but-unconfirmed direct-to-S3 uploads."""

from datetime import timedelta
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from scanning import services
from scanning.factories import ScanFactory, UserFactory, VolumeFactory
from scanning.models import PendingUpload, Status, UploadAction


def _make_pending(scan, action=UploadAction.UPLOAD_ONLY, minutes_old=0):
    pending = PendingUpload.objects.create(
        scan=scan,
        s3_key=f"processing/{scan.pk}/x/1/1.original.pdf",
        expected_size=1024,
        action=action,
        created_by=UserFactory(),
    )
    if minutes_old:
        old = timezone.now() - timedelta(minutes=minutes_old)
        PendingUpload.objects.filter(pk=pending.pk).update(date_created=old)
    return pending


class TestRecoverPendingUpload(TestCase):
    """services.recover_pending_upload attaches a landed-but-unconfirmed file."""

    def test_recovers_and_replays_validate_action(self):
        scan = ScanFactory(original_pdf="", status=Status.UPLOADED)
        self.assertFalse(scan.original_pdf.name)
        pending = _make_pending(scan, action=UploadAction.UPLOAD_VALIDATE)

        with patch(
            "scanning.s3_sync.verify_uploaded_object", return_value=True
        ):
            recovered = services.recover_pending_upload(pending)

        self.assertTrue(recovered)
        scan.refresh_from_db()
        self.assertTrue(scan.original_pdf.name.endswith(".original.pdf"))
        self.assertEqual(scan.status, Status.QUEUED)
        self.assertFalse(PendingUpload.objects.filter(pk=pending.pk).exists())

    def test_recovers_upload_only_and_queues_it(self):
        """An upload-only volume is queued too, for shard + convert.

        It used to stay UPLOADED, which meant it was never claimed and
        so never got a ``bitonal.pdf`` (#176). What the pipeline does
        today needs doing whatever the uploader chose; only the message
        distinguishes the two actions while validation is disconnected.
        """
        scan = ScanFactory(original_pdf="", status=Status.UPLOADED)
        pending = _make_pending(scan, action=UploadAction.UPLOAD_ONLY)

        with patch(
            "scanning.s3_sync.verify_uploaded_object", return_value=True
        ):
            recovered = services.recover_pending_upload(pending)

        self.assertTrue(recovered)
        scan.refresh_from_db()
        self.assertTrue(scan.original_pdf.name)
        self.assertEqual(scan.status, Status.QUEUED)
        self.assertIn("conversion", scan.progress_message)

    def test_no_recover_when_object_missing(self):
        scan = ScanFactory(original_pdf="", status=Status.UPLOADED)
        pending = _make_pending(scan)

        with patch(
            "scanning.s3_sync.verify_uploaded_object", return_value=False
        ):
            recovered = services.recover_pending_upload(pending)

        self.assertFalse(recovered)
        scan.refresh_from_db()
        self.assertFalse(scan.original_pdf.name)
        self.assertTrue(PendingUpload.objects.filter(pk=pending.pk).exists())

    def test_no_recover_when_scan_already_linked(self):
        # A re-upload's scan already has a confirmed original; recovery must
        # not touch it (and must not even hit S3).
        scan = ScanFactory(original_pdf="original_scans/x.original.pdf")
        self.assertTrue(scan.original_pdf.name)
        pending = _make_pending(scan)

        with patch("scanning.s3_sync.verify_uploaded_object") as mock_verify:
            recovered = services.recover_pending_upload(pending)

        self.assertFalse(recovered)
        mock_verify.assert_not_called()

    def test_recovered_part_upload_flags_the_volume(self):
        """A recovered upload is a completed upload (#178).

        The tab died before confirm, so the view never ran. The part
        label is still on the scan, and the volume must end up flagged
        exactly as a confirmed upload would leave it.
        """
        volume = VolumeFactory()
        scan = ScanFactory(
            volume_obj=volume,
            reporter=volume.reporter,
            volume=volume.volume_number,
            part_label="B",
            original_pdf="",
            status=Status.UPLOADED,
        )
        self.assertFalse(volume.is_partial)
        pending = _make_pending(scan)

        with patch(
            "scanning.s3_sync.verify_uploaded_object", return_value=True
        ):
            recovered = services.recover_pending_upload(pending)

        self.assertTrue(recovered)
        volume.refresh_from_db()
        self.assertTrue(volume.is_partial)

    def test_recovered_upload_without_part_label_leaves_volume_alone(self):
        volume = VolumeFactory()
        scan = ScanFactory(
            volume_obj=volume,
            reporter=volume.reporter,
            volume=volume.volume_number,
            part_label="",
            original_pdf="",
            status=Status.UPLOADED,
        )
        pending = _make_pending(scan)

        with patch(
            "scanning.s3_sync.verify_uploaded_object", return_value=True
        ):
            services.recover_pending_upload(pending)

        volume.refresh_from_db()
        self.assertFalse(volume.is_partial)


class TestRecoverCommand(TestCase):
    """The recover_pending_uploads command recovers eligible rows."""

    def test_recovers_aged_pending(self):
        scan = ScanFactory(original_pdf="", status=Status.UPLOADED)
        pending = _make_pending(
            scan, action=UploadAction.UPLOAD_VALIDATE, minutes_old=30
        )

        with patch(
            "scanning.s3_sync.verify_uploaded_object", return_value=True
        ):
            call_command("recover_pending_uploads")

        self.assertFalse(PendingUpload.objects.filter(pk=pending.pk).exists())
        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.QUEUED)

    def test_skips_too_recent_pending(self):
        scan = ScanFactory(original_pdf="", status=Status.UPLOADED)
        pending = _make_pending(scan, minutes_old=1)

        with patch(
            "scanning.s3_sync.verify_uploaded_object", return_value=True
        ):
            call_command("recover_pending_uploads")  # default 5-min grace

        # Too recent to touch (might still confirm normally).
        self.assertTrue(PendingUpload.objects.filter(pk=pending.pk).exists())
