"""Tests for the make_dev_data seeding command."""

import pathlib
import tempfile

from django.core.management import call_command
from django.test import TestCase, override_settings

from scanning.models import Scan, Status

MEDIA_ROOT = tempfile.mkdtemp()


@override_settings(
    DEVELOPMENT=True,
    RUNPOD_ENABLED=False,
    MEDIA_ROOT=MEDIA_ROOT,
)
class TestMakeDevData(TestCase):
    """make_dev_data seeds scans with a viewable preview."""

    def test_seeds_reviewable_scan(self):
        call_command("make_dev_data", count=1)

        scan = Scan.objects.get()
        # The scan is left in a reviewable state, not stuck "processing".
        self.assertEqual(scan.status, Status.PENDING_REVIEW)
        # A real, renderable bitonal preview is written to the output dir
        # (what serve_scan_pdf serves), plus the original for crops.
        output_dir = pathlib.Path(scan.output_dir)
        bitonal = output_dir / "bitonal.pdf"
        self.assertTrue(bitonal.is_file())
        self.assertTrue(bitonal.read_bytes().startswith(b"%PDF"))
        self.assertTrue(scan.original_pdf.name.endswith(".original.pdf"))
        # pdf_path resolves to the seeded original (used by crops).
        self.assertTrue(pathlib.Path(scan.pdf_path).is_file())

    def test_skips_when_not_development(self):
        with override_settings(DEVELOPMENT=False):
            call_command("make_dev_data", count=1)
        self.assertFalse(Scan.objects.exists())
