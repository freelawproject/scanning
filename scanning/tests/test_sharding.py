"""Tests for scanning.sharding (issue #164).

S3 is inert here: these tests run under ``TESTING``, where every
``s3_sync`` helper short-circuits, so ``ensure_shards`` exercises the
local (DEVELOPMENT-like) path. The S3 side of sharding -- prefix layout,
manifest-last upload ordering, sync exclusion -- is covered in
``test_s3_sync.py`` with mocked boto3.
"""

import io
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import fitz
from django.test import SimpleTestCase, TestCase, override_settings
from PIL import Image

from scanning import services, sharding
from scanning.factories import ReporterFactory, ScanFactory
from scanning.models import Status

MEDIA_ROOT = tempfile.mkdtemp()


def write_image_volume(path: Path, pages: int = 6) -> None:
    """Write a small scan-like PDF: one unique image per page.

    Each page carries a distinct image so the raw-image-stream digest
    check in ``verify_shards`` actually distinguishes pages -- identical
    images would let a page-swapping bug pass.

    :param path: Where to write the PDF.
    :param pages: Number of pages.
    """
    doc = fitz.open()
    for i in range(pages):
        img = Image.new("L", (60, 80), color=255)
        for x in range(60):
            img.putpixel((x, (7 * i + x) % 80), 0)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        page = doc.new_page(width=612, height=792)
        page.insert_image(fitz.Rect(72, 100, 540, 700), stream=buf.getvalue())
    doc.save(str(path))
    doc.close()


class TestPlanShards(SimpleTestCase):
    def assert_covers(self, ranges, page_count):
        """Ranges must be contiguous, 0-based, and cover every page once."""
        self.assertEqual(ranges[0][0], 0)
        for (_, prev_end), (next_start, _) in zip(ranges, ranges[1:]):
            self.assertEqual(next_start, prev_end + 1)
        self.assertEqual(ranges[-1][1], page_count - 1)

    def test_single_shard_when_under_target(self):
        ranges = sharding.plan_shards(50, 10_000, target_bytes=20_000)
        self.assertEqual(ranges, [(0, 49)])

    def test_shard_count_follows_byte_size(self):
        # 1000 bytes at a 300-byte target -> ceil = 4 shards, and the 10
        # pages spread as 3+3+2+2 (balanced, no tiny tail shard).
        ranges = sharding.plan_shards(10, 1000, target_bytes=300)
        self.assertEqual(ranges, [(0, 2), (3, 5), (6, 7), (8, 9)])
        self.assert_covers(ranges, 10)

    def test_exact_multiple_splits_evenly(self):
        ranges = sharding.plan_shards(8, 400, target_bytes=100)
        self.assertEqual(ranges, [(0, 1), (2, 3), (4, 5), (6, 7)])

    def test_shard_count_capped_at_page_count(self):
        # A 3-page file 10x over the target still can't make more than
        # 3 shards.
        ranges = sharding.plan_shards(3, 1000, target_bytes=100)
        self.assertEqual(ranges, [(0, 0), (1, 1), (2, 2)])

    def test_single_page_document(self):
        ranges = sharding.plan_shards(1, 500_000_000, target_bytes=100)
        self.assertEqual(ranges, [(0, 0)])

    def test_invalid_inputs_raise(self):
        with self.assertRaises(sharding.ShardingError):
            sharding.plan_shards(0, 1000, target_bytes=100)
        with self.assertRaises(sharding.ShardingError):
            sharding.plan_shards(10, 0, target_bytes=100)
        with self.assertRaises(sharding.ShardingError):
            sharding.plan_shards(10, 1000, target_bytes=0)


class TestShardPdfAndVerify(SimpleTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.source = self.tmp / "vol.original.pdf"
        write_image_volume(self.source, pages=6)
        # Pick a target that forces exactly 3 shards of 2 pages.
        self.target = -(-self.source.stat().st_size // 3)  # ceil(size/3)
        self.shard_dir = self.tmp / "shards"

    def test_shard_pdf_writes_files_matching_manifest(self):
        manifest = sharding.shard_pdf(self.source, self.shard_dir, self.target)

        self.assertEqual(len(manifest["shards"]), 3)
        self.assertEqual(
            [s["name"] for s in manifest["shards"]],
            ["0001.pdf", "0002.pdf", "0003.pdf"],
        )
        self.assertEqual(sum(s["page_count"] for s in manifest["shards"]), 6)
        self.assertEqual(manifest["source"]["page_count"], 6)
        self.assertEqual(
            manifest["source"]["size_bytes"], self.source.stat().st_size
        )
        for entry in manifest["shards"]:
            path = self.shard_dir / entry["name"]
            self.assertEqual(path.stat().st_size, entry["size_bytes"])
            with fitz.open(str(path)) as doc:
                self.assertEqual(doc.page_count, entry["page_count"])

    def test_round_trip_verifies(self):
        manifest = sharding.shard_pdf(self.source, self.shard_dir, self.target)
        # Must not raise: geometry and raw image streams are identical.
        sharding.verify_shards(self.source, self.shard_dir, manifest)

    def test_verify_rejects_swapped_shard_content(self):
        manifest = sharding.shard_pdf(self.source, self.shard_dir, self.target)
        # Replace shard 2 with a copy of shard 1: same page count and
        # geometry, wrong pages. Only the image-stream digests catch it.
        shutil.copy(self.shard_dir / "0001.pdf", self.shard_dir / "0002.pdf")
        with self.assertRaisesRegex(
            sharding.ShardingError, "image streams differ"
        ):
            sharding.verify_shards(self.source, self.shard_dir, manifest)

    def test_verify_rejects_missing_shard(self):
        manifest = sharding.shard_pdf(self.source, self.shard_dir, self.target)
        (self.shard_dir / "0003.pdf").unlink()
        with self.assertRaisesRegex(
            sharding.ShardingError, "missing shard file"
        ):
            sharding.verify_shards(self.source, self.shard_dir, manifest)

    def test_verify_rejects_gap_in_ranges(self):
        manifest = sharding.shard_pdf(self.source, self.shard_dir, self.target)
        manifest["shards"].pop(1)
        with self.assertRaisesRegex(sharding.ShardingError, "not contiguous"):
            sharding.verify_shards(self.source, self.shard_dir, manifest)


@override_settings(
    MEDIA_ROOT=MEDIA_ROOT,
    DEVELOPMENT=True,
    SHARDING_ENABLED=True,
    SHARD_TARGET_BYTES=1,  # every page over target -> one shard per page
)
class TestEnsureShards(TestCase):
    def _scan_with_volume(self, pages=4):
        """Build a scan whose original PDF is a real multipage volume.

        Mirrors the prod layout ``Scan.pdf_path`` resolves first: the
        original sits in ``output_dir`` under its FileField basename.

        :param pages: Page count of the synthetic volume.
        :returns: The scan.
        """
        reporter = ReporterFactory(short_name="tc")
        scan = ScanFactory(
            reporter=reporter, volume=164, start_page=1, end_page=pages
        )
        output_dir = Path(scan.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        original = output_dir / Path(scan.original_pdf.name).name
        write_image_volume(original, pages=pages)
        return scan

    def test_first_run_creates_shards_and_manifest(self):
        scan = self._scan_with_volume(pages=4)
        manifest = sharding.ensure_shards(scan)

        shards_dir = Path(scan.output_dir) / "shards"
        self.assertEqual(len(manifest["shards"]), 4)
        # S3 is inert under TESTING, so the local shard files remain.
        self.assertEqual(len(list(shards_dir.glob("*.pdf"))), 4)
        stored = json.loads((shards_dir / "manifest.json").read_text())
        self.assertEqual(stored["source"], manifest["source"])
        self.assertIn("split_seconds", stored["timings"])
        self.assertIn("verify_seconds", stored["timings"])

    def test_second_run_reuses_manifest(self):
        scan = self._scan_with_volume(pages=4)
        first = sharding.ensure_shards(scan)

        with patch("scanning.sharding.shard_pdf") as mock_shard:
            second = sharding.ensure_shards(scan)

        mock_shard.assert_not_called()
        self.assertEqual(second["source"], first["source"])

    def test_source_change_triggers_full_reshard(self):
        scan = self._scan_with_volume(pages=4)
        first = sharding.ensure_shards(scan)
        self.assertEqual(first["source"]["page_count"], 4)

        # A smart page edit rewrites the original in place. Edits do not
        # refresh shards themselves; the next full-volume run lands here
        # with a stale fingerprint and the whole set is re-cut.
        original = Path(scan.pdf_path)
        with fitz.open(str(original)) as doc:
            doc.delete_page(0)
            doc.save(str(original) + ".new")
        Path(str(original) + ".new").replace(original)

        second = sharding.ensure_shards(scan)
        self.assertEqual(second["source"]["page_count"], 3)
        self.assertEqual(
            [s["name"] for s in second["shards"]],
            ["0001.pdf", "0002.pdf", "0003.pdf"],
        )
        shards_dir = Path(scan.output_dir) / "shards"
        self.assertEqual(len(list(shards_dir.glob("*.pdf"))), 3)

    @override_settings(SHARDING_ENABLED=False)
    def test_disabled_returns_none(self):
        scan = self._scan_with_volume(pages=2)
        self.assertIsNone(sharding.ensure_shards(scan))
        self.assertFalse((Path(scan.output_dir) / "shards").exists())

    def test_missing_original_raises(self):
        scan = ScanFactory(original_pdf=None)
        with self.assertRaisesRegex(sharding.ShardingError, "no original PDF"):
            sharding.ensure_shards(scan)

    def test_failure_leaves_no_manifest_or_shards(self):
        scan = self._scan_with_volume(pages=4)
        with patch(
            "scanning.sharding.verify_shards",
            side_effect=sharding.ShardingError("boom"),
        ):
            with self.assertRaises(sharding.ShardingError):
                sharding.ensure_shards(scan)
        # No half-written multi-GB set left behind; the next run retries.
        self.assertFalse((Path(scan.output_dir) / "shards").exists())

    def test_pipeline_wrapper_propagates_failures(self):
        # Sharding is a regular pipeline stage: a failure must reach
        # _handle_pipeline_exception (which marks the scan ERROR), not
        # be swallowed.
        scan = self._scan_with_volume(pages=2)
        with patch(
            "scanning.sharding.ensure_shards",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                services._ensure_shards(scan)

    def test_full_pipeline_runs_the_sharding_stage(self):
        """Pin the call site: run_full_pipeline shards before bitonal."""
        scan = self._scan_with_volume(pages=2)
        bitonal = Path(scan.output_dir) / "bitonal.pdf"
        with (
            patch("scanning.services._ensure_shards") as mock_shards,
            patch("scanning.services._ensure_bitonal", return_value=bitonal),
            patch("scanning.services._run_yolo"),
            patch(
                "scanning.services._import_detections_from_json",
                return_value=[],
            ),
            patch("scanning.services.run_paddleocr_validation"),
            patch("scanning.services._re_pair_opinions", return_value=[]),
            # run_full_pipeline drops DB connections for the daemon; a
            # real close inside the test transaction would wreck it.
            patch("django.db.connections.close_all"),
        ):
            services.run_full_pipeline(scan.pk)

        mock_shards.assert_called_once()
        self.assertEqual(mock_shards.call_args.args[0].pk, scan.pk)
        scan.refresh_from_db()
        # PENDING_REVIEW proves the pipeline ran to completion; an
        # exception anywhere would have left the scan in ERROR instead.
        self.assertEqual(scan.status, Status.PENDING_REVIEW)
