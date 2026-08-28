"""Tests for ``scanning.bitonal``: the skip check and the merge.

S3 is inert under ``TESTING``, so the merge's downloads are patched to
copy from a local directory. Under test:

- ``source_is_bitonal``, which decides whether doctor is needed at all
- the merge's page arithmetic, all that stands between a misassembled
  volume and the viewer
- the order of the park and the CONSUMED write, which keeps a finished
  conversion from being applied twice or lost
"""

import io
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import fitz
from django.test import TestCase
from PIL import Image

from scanning import bitonal, jobs
from scanning.factories import ScanFactory
from scanning.models import ExternalJob, JobStatus, Scan, Status
from scanning.tests.test_jobs import make_manifest
from scanning.utils import ensure_output_dir

MEDIA_ROOT = tempfile.mkdtemp()


def write_gray_volume(path: Path, pages: int = 3) -> None:
    """Write a scan-like PDF whose pages carry 8-bit grayscale images.

    :param path: Where to write the PDF.
    :param pages: Number of pages.
    """
    doc = fitz.open()
    for index in range(pages):
        img = Image.new("L", (40, 50), color=200 - index)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        page = doc.new_page(width=612, height=792)
        page.insert_image(fitz.Rect(72, 100, 540, 700), stream=buf.getvalue())
    doc.save(str(path))
    doc.close()


def write_bitonal_volume(path: Path, pages: int = 3) -> None:
    """Write a scan-like PDF whose pages carry 1-bit CCITT G4 images.

    What a Hein scan already looks like, and therefore what the skip
    check has to recognize.

    :param path: Where to write the PDF.
    :param pages: Number of pages.
    """
    doc = fitz.open()
    for index in range(pages):
        img = Image.new("L", (40, 50), color=255)
        for x in range(40):
            img.putpixel((x, (3 * index + x) % 50), 0)
        buf = io.BytesIO()
        img.convert("1").save(buf, format="TIFF", compression="group4")
        page = doc.new_page(width=612, height=792)
        page.insert_image(fitz.Rect(72, 100, 540, 700), stream=buf.getvalue())
    doc.save(str(path))
    doc.close()


class TestSourceIsBitonal(TestCase):
    """Deciding whether the conversion is worth running at all."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_1bpc_volume_is_recognized(self):
        path = self.tmp / "bitonal.pdf"
        write_bitonal_volume(path, pages=3)

        self.assertTrue(bitonal.source_is_bitonal(path))

    def test_a_grayscale_volume_needs_converting(self):
        path = self.tmp / "gray.pdf"
        write_gray_volume(path, pages=3)

        self.assertFalse(bitonal.source_is_bitonal(path))

    def test_one_deep_page_is_enough_to_convert_the_volume(self):
        """The check is per volume, so a mixed scan is converted."""
        bitonal_path = self.tmp / "b.pdf"
        gray_path = self.tmp / "g.pdf"
        write_bitonal_volume(bitonal_path, pages=2)
        write_gray_volume(gray_path, pages=1)
        mixed = self.tmp / "mixed.pdf"
        with fitz.open(str(bitonal_path)) as first:
            with fitz.open(str(gray_path)) as second:
                first.insert_pdf(second)
                first.save(str(mixed))

        self.assertFalse(bitonal.source_is_bitonal(mixed))

    def test_a_page_with_no_image_is_not_assumed_bitonal(self):
        """Conservative: it cannot be *shown* to be 1-bit, so convert."""
        bitonal_path = self.tmp / "b.pdf"
        write_bitonal_volume(bitonal_path, pages=1)
        with_blank = self.tmp / "with_blank.pdf"
        with fitz.open(str(bitonal_path)) as doc:
            doc.new_page(width=612, height=792)
            doc.save(str(with_blank))

        self.assertFalse(bitonal.source_is_bitonal(with_blank))

    def test_a_single_page_volume_is_judged_on_that_page(self):
        path = self.tmp / "one.pdf"
        write_bitonal_volume(path, pages=1)

        self.assertTrue(bitonal.source_is_bitonal(path))


class ConvertJobsMixin:
    """Builds a scan whose conversion jobs have results on 'S3'."""

    def build(self, shard_count=3, pages_per_shard=2, result_pages=None):
        """Create a scan, its jobs, and a converted PDF per shard.

        The result objects live in a local directory that a patched
        ``download_object`` copies from, so the merge exercises its real
        code path without S3.

        :param shard_count: Shards to create.
        :param pages_per_shard: Pages each shard covers.
        :param result_pages: Optional per-shard override of how many
            pages the converted result actually has, for the mismatch
            tests.
        :returns: ``(scan, jobs)``.
        """
        self.store = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.store, True)

        scan = ScanFactory(
            status=Status.AWAITING,
            page_count=shard_count * pages_per_shard,
        )
        ensure_output_dir(scan)
        rows = jobs.ensure_convert_jobs(
            scan, make_manifest(shard_count, pages_per_shard)
        )
        for index, job in enumerate(rows):
            job.status = JobStatus.COMPLETED
            job.result_key = f"jobs/convert/bitonal/r1-s{index}-a1.pdf"
            job.save()
            pages = (
                result_pages[index]
                if result_pages is not None
                else pages_per_shard
            )
            write_bitonal_volume(
                self.store / f"s{index}.pdf", pages=max(pages, 0)
            )

        def _download(key, dest_path):
            index = int(key.split("-s")[1].split("-")[0])
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(self.store / f"s{index}.pdf", dest_path)

        patcher = patch(
            "scanning.s3_sync.download_object", side_effect=_download
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return scan, bitonal.live_convert_jobs(scan)


class TestMergeConvertResults(ConvertJobsMixin, TestCase):
    """Reassembling the shards."""

    def test_shards_are_merged_in_order_into_bitonal_pdf(self):
        scan, rows = self.build(shard_count=3, pages_per_shard=2)

        path = bitonal.merge_convert_results(scan, rows)

        self.assertEqual(path.name, "bitonal.pdf")
        self.assertEqual(path, Path(scan.output_dir) / "bitonal.pdf")
        with fitz.open(str(path)) as merged:
            self.assertEqual(merged.page_count, 6)

    def test_the_merged_volume_is_uploaded(self):
        scan, rows = self.build(shard_count=2, pages_per_shard=1)

        with patch("scanning.s3_sync.upload_file_to_s3") as upload:
            bitonal.merge_convert_results(scan, rows)

        upload.assert_called_once_with(scan, "bitonal.pdf")

    def test_merging_twice_gives_the_same_volume(self):
        """Idempotent, so a crash between merge and park costs nothing."""
        scan, rows = self.build(shard_count=2, pages_per_shard=2)

        first = bitonal.merge_convert_results(scan, rows)
        with fitz.open(str(first)) as merged:
            pages = merged.page_count
        bitonal.merge_convert_results(scan, rows)

        with fitz.open(str(first)) as merged:
            self.assertEqual(merged.page_count, pages)

    def test_a_shard_converted_to_the_wrong_page_count_is_refused(self):
        """The check doctor cannot make: that *this* shard came back."""
        scan, rows = self.build(
            shard_count=3, pages_per_shard=2, result_pages=[2, 1, 2]
        )

        with self.assertRaises(bitonal.BitonalMergeError) as caught:
            bitonal.merge_convert_results(scan, rows)

        self.assertIn("shard 1", str(caught.exception))
        self.assertFalse(
            (Path(scan.output_dir) / "bitonal.pdf").exists(),
            "a volume that failed verification must not be published",
        )

    def test_a_gap_in_the_shard_sequence_is_refused(self):
        scan, rows = self.build(shard_count=3, pages_per_shard=2)
        ExternalJob.objects.filter(pk=rows[1].pk).delete()

        with self.assertRaises(bitonal.BitonalMergeError):
            bitonal.merge_convert_results(
                scan, bitonal.live_convert_jobs(scan)
            )

    def test_a_row_with_no_result_key_is_refused(self):
        scan, rows = self.build(shard_count=2, pages_per_shard=2)
        ExternalJob.objects.filter(pk=rows[1].pk).update(result_key="")

        with self.assertRaises(bitonal.BitonalMergeError):
            bitonal.merge_convert_results(
                scan, bitonal.live_convert_jobs(scan)
            )

    def test_a_volume_short_of_the_original_page_count_is_refused(self):
        """Belt and braces on top of the per-shard check."""
        scan, rows = self.build(shard_count=2, pages_per_shard=2)
        for job in rows:
            job.input_manifest["source_page_count"] = 99
            job.save(update_fields=["input_manifest"])

        with self.assertRaises(bitonal.BitonalMergeError) as caught:
            bitonal.merge_convert_results(
                scan, bitonal.live_convert_jobs(scan)
            )

        self.assertIn("99", str(caught.exception))

    def test_no_jobs_is_an_error_not_an_empty_volume(self):
        scan = ScanFactory()

        with self.assertRaises(bitonal.BitonalMergeError):
            bitonal.merge_convert_results(scan, [])


class TestFinishReadyScans(ConvertJobsMixin, TestCase):
    """Applying a finished conversion."""

    def test_a_completed_run_is_merged_parked_and_consumed(self):
        scan, _ = self.build(shard_count=2, pages_per_shard=2)

        with patch("scanning.s3_sync.upload_file_to_s3"):
            finished = bitonal.finish_ready_scans()

        scan.refresh_from_db()
        self.assertEqual(finished, 1)
        self.assertEqual(scan.status, Status.AWAITING_VALIDATION)
        self.assertIn("converted", scan.progress_message)
        self.assertEqual(
            {job.status for job in bitonal.live_convert_jobs(scan)},
            {JobStatus.CONSUMED},
        )
        self.assertTrue((Path(scan.output_dir) / "bitonal.pdf").exists())

    def test_the_page_count_is_left_alone(self):
        """run_full_pipeline owns it; a second writer would drift."""
        scan, _ = self.build(shard_count=2, pages_per_shard=2)
        ExternalJob.objects.filter(scan=scan).update(shard_count=2)
        scan.page_count = 4
        scan.save(update_fields=["page_count"])

        with patch("scanning.s3_sync.upload_file_to_s3"):
            bitonal.finish_ready_scans()

        scan.refresh_from_db()
        self.assertEqual(scan.page_count, 4)

    def test_consumed_results_are_swept_from_s3(self):
        """They duplicate every byte of the bitonal.pdf they became."""
        scan, rows = self.build(shard_count=2, pages_per_shard=1)

        with patch("scanning.s3_sync.upload_file_to_s3"):
            with patch("scanning.s3_sync.delete_objects") as delete:
                bitonal.finish_ready_scans()

        delete.assert_called_once()
        self.assertEqual(
            sorted(delete.call_args.args[0]),
            sorted(job.result_key for job in rows),
        )

    def test_a_run_still_in_flight_is_left_alone(self):
        scan, rows = self.build(shard_count=3, pages_per_shard=1)
        ExternalJob.objects.filter(pk=rows[2].pk).update(
            status=JobStatus.SUBMITTED
        )

        finished = bitonal.finish_ready_scans()

        scan.refresh_from_db()
        self.assertEqual(finished, 0)
        self.assertEqual(scan.status, Status.AWAITING)

    def test_a_run_with_a_pending_shard_is_left_alone(self):
        scan, rows = self.build(shard_count=2, pages_per_shard=1)
        ExternalJob.objects.filter(pk=rows[1].pk).update(
            status=JobStatus.PENDING
        )

        finished = bitonal.finish_ready_scans()

        scan.refresh_from_db()
        self.assertEqual(finished, 0)
        self.assertEqual(scan.status, Status.AWAITING)

    def test_a_dead_shard_errors_the_scan_and_names_the_code(self):
        scan, rows = self.build(shard_count=2, pages_per_shard=1)
        ExternalJob.objects.filter(pk=rows[1].pk).update(
            status=JobStatus.FAILED,
            error_code="CONVERSION_FAILED",
            error_message="pdftoppm died",
        )

        with self.assertLogs("scanning.bitonal", level="ERROR"):
            finished = bitonal.finish_ready_scans()

        scan.refresh_from_db()
        self.assertEqual(finished, 1)
        self.assertEqual(scan.status, Status.ERROR)
        self.assertIn("CONVERSION_FAILED", scan.progress_message)

    def test_a_merge_failure_errors_the_scan(self):
        scan, _ = self.build(
            shard_count=2, pages_per_shard=2, result_pages=[2, 1]
        )

        with self.assertLogs("scanning.bitonal", level="ERROR"):
            bitonal.finish_ready_scans()

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.ERROR)
        self.assertIn("assemble", scan.progress_message)

    def test_a_scan_that_left_awaiting_is_not_stomped(self):
        """An admin action or a cancel outranks a finished job."""
        scan, rows = self.build(shard_count=2, pages_per_shard=1)
        scan.status = Status.CANCELLED
        scan.save(update_fields=["status"])

        finished = bitonal.finish_ready_scans()

        scan.refresh_from_db()
        self.assertEqual(finished, 0)
        self.assertEqual(scan.status, Status.CANCELLED)
        self.assertEqual(
            {job.status for job in bitonal.live_convert_jobs(scan)},
            {JobStatus.COMPLETED},
        )

    def test_only_the_live_run_decides(self):
        """A previous run's failures must not error a re-run.

        The first run failed on a shard set that no longer exists; the
        volume was re-cut (different shard keys), so ``ensure_convert_jobs``
        opened run 2. Reading the union of both runs would error a scan
        whose current work actually succeeded.
        """
        scan, rows = self.build(shard_count=1, pages_per_shard=4)
        ExternalJob.objects.filter(pk=rows[0].pk).update(
            status=JobStatus.FAILED, error_code="CONVERSION_FAILED"
        )

        second = jobs.ensure_convert_jobs(
            scan, make_manifest(shard_count=2, pages_per_shard=2)
        )
        self.assertEqual({job.run for job in second}, {2})
        for index, job in enumerate(second):
            ExternalJob.objects.filter(pk=job.pk).update(
                status=JobStatus.COMPLETED,
                result_key=f"jobs/convert/bitonal/r2-s{index}-a1.pdf",
            )
            write_bitonal_volume(self.store / f"s{index}.pdf", pages=2)

        with patch("scanning.s3_sync.upload_file_to_s3"):
            finished = bitonal.finish_ready_scans()

        scan.refresh_from_db()
        self.assertEqual(finished, 1)
        self.assertEqual(scan.status, Status.AWAITING_VALIDATION)


class TestAlreadyConsumedRun(ConvertJobsMixin, TestCase):
    """A run whose results were already merged and swept."""

    def test_a_consumed_run_is_parked_without_re_merging(self):
        """The merge deleted its results, so re-reading them 404s.

        Reachable if something puts a converted scan back into AWAITING.
        Erroring over objects we deleted on purpose is the wrong answer:
        the work is done.
        """
        scan, rows = self.build(shard_count=2, pages_per_shard=1)
        ExternalJob.objects.filter(scan=scan).update(status=JobStatus.CONSUMED)

        with patch("scanning.s3_sync.download_object") as download:
            finished = bitonal.finish_ready_scans()

        scan.refresh_from_db()
        download.assert_not_called()
        self.assertEqual(finished, 1)
        self.assertEqual(scan.status, Status.AWAITING_VALIDATION)


class TestFinishedCount(ConvertJobsMixin, TestCase):
    """The count the collect tick reports."""

    def test_a_merge_failure_on_a_scan_that_moved_counts_nothing(self):
        """Nothing changed, so reporting it as finished would be a lie."""
        scan, _ = self.build(shard_count=2, pages_per_shard=2)

        def _fail_after_someone_else_takes_the_scan(*args, **kwargs):
            Scan.objects.filter(pk=scan.pk).update(status=Status.CANCELLED)
            raise bitonal.BitonalMergeError("boom")

        with patch(
            "scanning.bitonal.merge_convert_results",
            side_effect=_fail_after_someone_else_takes_the_scan,
        ):
            with self.assertLogs("scanning.bitonal", level="ERROR"):
                finished = bitonal.finish_ready_scans()

        scan.refresh_from_db()
        self.assertEqual(finished, 0)
        self.assertEqual(scan.status, Status.CANCELLED)


class TestDurationLogging(ConvertJobsMixin, TestCase):
    """Assembly and end-to-end timings, readable from the logs."""

    def test_the_merge_logs_how_long_assembly_took(self):
        scan, rows = self.build(shard_count=2, pages_per_shard=2)

        with patch("scanning.s3_sync.upload_file_to_s3"):
            with self.assertLogs("scanning.bitonal", level="INFO") as logs:
                bitonal.merge_convert_results(scan, rows)

        line = "\n".join(logs.output)
        self.assertIn("Merged 2 converted shard(s)", line)
        self.assertRegex(line, r"in \d+\.\d+s")

    def test_finishing_logs_the_whole_stage(self):
        """From the rows being created to the scan leaving AWAITING."""
        scan, _ = self.build(shard_count=3, pages_per_shard=4)

        with patch("scanning.s3_sync.upload_file_to_s3"):
            with self.assertLogs("scanning.bitonal", level="INFO") as logs:
                bitonal.finish_ready_scans()

        line = "\n".join(logs.output)
        self.assertIn(f"Bitonal stage done for scan {scan.pk}", line)
        self.assertIn("3 shard(s), 12 page(s)", line)
        self.assertRegex(line, r"pages/s")
