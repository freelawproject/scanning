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
        # The first seeded scan shows the #154 ready state.
        self.assertEqual(
            scan.status, Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )
        # A real, renderable bitonal preview is written to the output dir
        # (what serve_scan_pdf serves), plus the original for crops.
        output_dir = pathlib.Path(scan.output_dir)
        bitonal = output_dir / "bitonal.pdf"
        self.assertTrue(bitonal.is_file())
        self.assertTrue(bitonal.read_bytes().startswith(b"%PDF"))
        self.assertTrue(scan.original_pdf.name.endswith(".original.pdf"))
        # pdf_path resolves to the seeded original (used by crops).
        self.assertTrue(pathlib.Path(scan.pdf_path).is_file())

    def test_seeds_one_example_of_each_review_state(self):
        """The seed shows the two #154 states, the two #263 ones and
        the legacy review status, so the badges and the step selection
        are visible in dev."""
        call_command("make_dev_data", count=5)

        statuses = list(
            Scan.objects.order_by("pk").values_list("status", flat=True)
        )
        self.assertEqual(
            statuses,
            [
                Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
                Status.PAGE_COMPLETENESS_REVIEW_DONE,
                Status.READY_FOR_REDACTION_REVIEW,
                Status.REDACTION_REVIEW_DONE,
                Status.PENDING_REVIEW,
            ],
        )

    def test_skips_when_not_development(self):
        with override_settings(DEVELOPMENT=False):
            call_command("make_dev_data", count=1)
        self.assertFalse(Scan.objects.exists())
