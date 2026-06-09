"""Tests for the ``import_scanlist`` management command."""

import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from scanning.factories import ReporterFactory
from scanning.models import Scan, Volume

HEADER = (
    "Slug\tVolume #\tStatus\tPriority\tFirst Page\tLast Page"
    "\tNotes\tAssigned To\tLocated\tAdvance Sheet/Volume\tBook"
)


def _row(
    slug="a",
    volume="100",
    status="Not Started",
    priority="HIGH",
    first="1",
    last="200",
    notes="",
    assigned="",
    located="",
    advance="",
    book="",
):
    return "\t".join(
        [
            slug,
            volume,
            status,
            priority,
            first,
            last,
            notes,
            assigned,
            located,
            advance,
            book,
        ]
    )


class TestImportScanlist(TestCase):
    """The command seeds Volumes only and skips existing ones."""

    def setUp(self):
        ReporterFactory(short_name="a", full_name="Atlantic Reporter")

    def _run(self, *rows, dry_run=False):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scanlist.csv"
            csv_path.write_text("\n".join([HEADER, *rows]) + "\n")
            out = StringIO()
            args = [str(csv_path)]
            if dry_run:
                args.append("--dry-run")
            call_command("import_scanlist", *args, stdout=out)
            return out.getvalue()

    def test_creates_volume_without_scan(self):
        self._run(_row(volume="100", first="1", last="200"))
        self.assertEqual(Scan.objects.count(), 0)
        volume = Volume.objects.get(volume_number=100)
        self.assertEqual(volume.expected_start_page, 1)
        self.assertEqual(volume.expected_end_page, 200)

    def test_skips_existing_volume_with_message(self):
        self._run(_row(volume="100"))
        output = self._run(_row(volume="100"))
        self.assertEqual(Volume.objects.filter(volume_number=100).count(), 1)
        self.assertIn("Skipping existing volume", output)

    def test_multi_row_volume_merges_page_range(self):
        # Two parts of the same volume in one run -> one Volume,
        # widened page range, flagged partial, no skip message.
        output = self._run(
            _row(volume="100A", first="1", last="100"),
            _row(volume="100B", first="101", last="250"),
        )
        self.assertEqual(Volume.objects.filter(volume_number=100).count(), 1)
        self.assertEqual(Scan.objects.count(), 0)
        volume = Volume.objects.get(volume_number=100)
        self.assertEqual(volume.expected_start_page, 1)
        self.assertEqual(volume.expected_end_page, 250)
        self.assertTrue(volume.is_partial)
        self.assertNotIn("Skipping existing volume", output)

    def test_dry_run_creates_nothing(self):
        self._run(_row(volume="100"), dry_run=True)
        self.assertEqual(Volume.objects.count(), 0)
        self.assertEqual(Scan.objects.count(), 0)
