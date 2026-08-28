"""Tests for the dots.mocr glue (issue #202).

S3 is inert under ``TESTING``, so the glue's downloads are patched to
copy envelopes from a local directory and its upload is captured. Under
test:

- the page arithmetic: shard-local ``page_no`` to volume ``page_index``
- the envelope checks, all that stands between a paid result and a
  document glued from the wrong bytes
- the finish pass: rows consumed, results kept, no scan status written,
  and a bounded retry of a glue that keeps failing
"""

import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from scanning import dots_mocr, s3_sync
from scanning.factories import ScanFactory
from scanning.models import ExternalJob, JobStatus
from scanning.tests.test_jobs import make_manifest


def make_page(page_no: int, text: str = "98") -> dict:
    """Build one per-page dict of the shape the worker reports.

    :param page_no: 0-based index inside the shard.
    :param text: Text for the page's one cell.
    :returns: A page dict.
    :rtype: dict
    """
    return {
        "page_no": page_no,
        "input_width": 1024,
        "input_height": 1536,
        "origin_width": 1700,
        "origin_height": 2200,
        "completion_tokens": 42,
        "filtered": False,
        "cells": [
            {
                "bbox": [323, 143, 364, 177],
                "category": "Page-header",
                "text": text,
            }
        ],
        "md": text,
        "duration_ms": 100,
    }


def make_envelope(job: ExternalJob, pages: list[dict], **overrides) -> dict:
    """Build the result envelope one worker attempt PUTs to S3.

    :param job: The row the envelope answers.
    :param pages: The payload's page dicts.
    :param overrides: Envelope fields to replace, for the check tests.
    :returns: An envelope dict.
    :rtype: dict
    """
    payload = {
        "pages": pages,
        "page_count": len(pages),
        "failed_pages": [p["page_no"] for p in pages if "error" in p],
        "duration_ms": 1000,
    }
    envelope = {
        "schema_version": 1,
        "action": "parse",
        "scan_pk": job.scan_id,
        "result_key": job.result_key,
        "payload": payload,
    }
    envelope.update(overrides)
    return envelope


class AnalyzeJobsMixin:
    """Builds a scan whose dots.mocr jobs have results on 'S3'."""

    def build(self, shard_count=3, pages_per_shard=2):
        """Create a scan, its jobs, and an envelope per shard.

        The envelopes live in a local directory that a patched
        ``download_object`` copies from, so the glue exercises its real
        code path without S3. A test that needs a broken envelope
        rewrites the shard's file through :meth:`write_envelope`.

        :param shard_count: Shards to create.
        :param pages_per_shard: Pages each shard covers.
        :returns: ``(scan, jobs)``.
        """
        self.store = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.store, True)

        scan = ScanFactory(page_count=shard_count * pages_per_shard)
        rows = dots_mocr.ensure_analyze_jobs(
            scan, make_manifest(shard_count, pages_per_shard)
        )
        for index, job in enumerate(rows):
            job.status = JobStatus.COMPLETED
            job.result_key = f"jobs/analyze/dots_mocr/r1-s{index}-a1.json"
            job.save()
            self.write_envelope(
                index,
                make_envelope(
                    job, [make_page(n) for n in range(pages_per_shard)]
                ),
            )

        def _download(key, dest_path):
            index = int(key.split("-s")[1].split("-")[0])
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(self.store / f"s{index}.json", dest_path)

        download = patch(
            "scanning.s3_sync.download_object", side_effect=_download
        )
        self.download = download.start()
        self.addCleanup(download.stop)
        active = patch("scanning.s3_sync.s3_active", return_value=True)
        active.start()
        self.addCleanup(active.stop)
        upload = patch(
            "scanning.s3_sync.upload_json_object", return_value=True
        )
        self.upload = upload.start()
        self.addCleanup(upload.stop)
        return scan, dots_mocr.live_analyze_jobs(scan)

    def write_envelope(self, index: int, envelope: dict) -> None:
        """Put ``envelope`` where shard ``index``'s download reads from.

        :param index: The shard index to answer for.
        :param envelope: The envelope to store.
        :return: None.
        """
        (self.store / f"s{index}.json").write_text(json.dumps(envelope))


class TestMergeDotsmocrResults(AnalyzeJobsMixin, TestCase):
    """Gluing the shard payloads."""

    def test_shards_are_glued_in_order_with_volume_page_indexes(self):
        scan, rows = self.build(shard_count=3, pages_per_shard=2)

        key = dots_mocr.merge_dotsmocr_results(scan, rows)

        self.upload.assert_called_once_with(key, self.upload.call_args[0][1])
        document = self.upload.call_args[0][1]
        self.assertEqual(
            [page["page_index"] for page in document["pages"]],
            list(range(6)),
        )
        self.assertEqual(
            [page["pdf_page"] for page in document["pages"]],
            list(range(1, 7)),
        )
        self.assertEqual(
            [page["shard_index"] for page in document["pages"]],
            [0, 0, 1, 1, 2, 2],
        )
        self.assertEqual(
            [page["page_no"] for page in document["pages"]],
            [0, 1, 0, 1, 0, 1],
        )

    def test_the_document_names_the_run_and_the_volume(self):
        scan, rows = self.build(shard_count=2, pages_per_shard=2)

        dots_mocr.merge_dotsmocr_results(scan, rows)

        document = self.upload.call_args[0][1]
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["engine"], "dots_mocr")
        self.assertEqual(document["scan_pk"], scan.pk)
        self.assertEqual(document["run"], 1)
        self.assertEqual(document["source_page_count"], 4)
        self.assertEqual(document["dpi"], dots_mocr.DPI)
        self.assertEqual(document["failed_pages"], [])

    def test_the_document_lands_at_a_run_scoped_key(self):
        scan, rows = self.build(shard_count=2, pages_per_shard=1)

        key = dots_mocr.merge_dotsmocr_results(scan, rows)

        self.assertEqual(
            key,
            f"{s3_sync.s3_processing_prefix(scan)}"
            f"jobs/analyze/dots_mocr/r1-volume.json",
        )

    def test_the_per_page_payload_survives_whole(self):
        """The glue is naive on purpose: nothing is trimmed."""
        scan, rows = self.build(shard_count=1, pages_per_shard=1)

        dots_mocr.merge_dotsmocr_results(scan, rows)

        page = self.upload.call_args[0][1]["pages"][0]
        self.assertEqual(page["cells"][0]["category"], "Page-header")
        self.assertEqual(page["md"], "98")
        self.assertEqual(page["origin_width"], 1700)
        self.assertFalse(page["filtered"])

    def test_shard_provenance_is_recorded_for_the_smart_glue(self):
        scan, rows = self.build(shard_count=2, pages_per_shard=3)

        dots_mocr.merge_dotsmocr_results(scan, rows)

        shards = self.upload.call_args[0][1]["shards"]
        self.assertEqual([entry["from_page"] for entry in shards], [0, 3])
        self.assertEqual(
            [entry["result_key"] for entry in shards],
            [job.result_key for job in rows],
        )

    def test_a_failed_page_keeps_its_slot(self):
        """#149 reads a missing page as ``detected=None``; the slot has
        to exist for that."""
        scan, rows = self.build(shard_count=2, pages_per_shard=2)
        self.write_envelope(
            1,
            make_envelope(
                rows[1], [make_page(0), {"page_no": 1, "error": "boom"}]
            ),
        )

        dots_mocr.merge_dotsmocr_results(scan, rows)

        document = self.upload.call_args[0][1]
        self.assertEqual(len(document["pages"]), 4)
        self.assertEqual(document["pages"][3]["error"], "boom")
        self.assertEqual(document["failed_pages"], [3])

    def test_gluing_twice_gives_the_same_document(self):
        """Idempotent, so a crash before the CONSUMED write costs
        nothing."""
        scan, rows = self.build(shard_count=2, pages_per_shard=2)

        dots_mocr.merge_dotsmocr_results(scan, rows)
        first = self.upload.call_args[0][1]
        dots_mocr.merge_dotsmocr_results(scan, rows)
        second = self.upload.call_args[0][1]

        first.pop("generated_at")
        second.pop("generated_at")
        self.assertEqual(first, second)

    def test_a_shard_short_of_its_pages_is_refused(self):
        scan, rows = self.build(shard_count=2, pages_per_shard=2)
        self.write_envelope(1, make_envelope(rows[1], [make_page(0)]))

        with self.assertRaises(dots_mocr.DotsMocrGlueError) as caught:
            dots_mocr.merge_dotsmocr_results(scan, rows)

        self.assertIn("shard 1", str(caught.exception))
        self.upload.assert_not_called()

    def test_a_gap_in_the_answered_pages_is_refused(self):
        scan, rows = self.build(shard_count=1, pages_per_shard=2)
        self.write_envelope(
            0, make_envelope(rows[0], [make_page(0), make_page(2)])
        )

        with self.assertRaises(dots_mocr.DotsMocrGlueError):
            dots_mocr.merge_dotsmocr_results(scan, rows)

    def test_a_gap_in_the_shard_sequence_is_refused(self):
        scan, rows = self.build(shard_count=3, pages_per_shard=2)
        ExternalJob.objects.filter(pk=rows[1].pk).delete()

        with self.assertRaises(dots_mocr.DotsMocrGlueError):
            dots_mocr.merge_dotsmocr_results(
                scan, dots_mocr.live_analyze_jobs(scan)
            )

    def test_a_row_with_no_result_key_is_refused(self):
        scan, rows = self.build(shard_count=2, pages_per_shard=2)
        ExternalJob.objects.filter(pk=rows[1].pk).update(result_key="")

        with self.assertRaises(dots_mocr.DotsMocrGlueError):
            dots_mocr.merge_dotsmocr_results(
                scan, dots_mocr.live_analyze_jobs(scan)
            )

    def test_an_envelope_for_another_scan_is_refused(self):
        scan, rows = self.build(shard_count=1, pages_per_shard=1)
        self.write_envelope(
            0, make_envelope(rows[0], [make_page(0)], scan_pk=scan.pk + 1)
        )

        with self.assertRaises(dots_mocr.DotsMocrGlueError) as caught:
            dots_mocr.merge_dotsmocr_results(scan, rows)

        self.assertIn("scan_pk", str(caught.exception))

    def test_an_unknown_result_schema_is_refused_naming_both(self):
        """A worker deployed ahead of the daemon must read as that, not
        as a bad volume."""
        scan, rows = self.build(shard_count=1, pages_per_shard=1)
        self.write_envelope(
            0, make_envelope(rows[0], [make_page(0)], schema_version=2)
        )

        with self.assertRaises(dots_mocr.DotsMocrGlueError) as caught:
            dots_mocr.merge_dotsmocr_results(scan, rows)

        self.assertIn("schema 2", str(caught.exception))
        self.assertIn("knows 1", str(caught.exception))

    def test_a_volume_short_of_the_original_page_count_is_refused(self):
        scan, rows = self.build(shard_count=2, pages_per_shard=2)
        for job in rows:
            job.input_manifest["source_page_count"] = 99
            job.save(update_fields=["input_manifest"])

        with self.assertRaises(dots_mocr.DotsMocrGlueError) as caught:
            dots_mocr.merge_dotsmocr_results(
                scan, dots_mocr.live_analyze_jobs(scan)
            )

        self.assertIn("99", str(caught.exception))

    def test_no_jobs_is_an_error_not_an_empty_document(self):
        scan = ScanFactory()

        with self.assertRaises(dots_mocr.DotsMocrGlueError):
            dots_mocr.merge_dotsmocr_results(scan, [])


class TestFinishReadyRuns(AnalyzeJobsMixin, TestCase):
    """The daemon pass that applies finished runs."""

    def test_a_completed_run_is_glued_and_consumed(self):
        scan, _ = self.build(shard_count=2, pages_per_shard=2)

        glued = dots_mocr.finish_ready_runs()

        self.assertEqual(glued, 1)
        rows = dots_mocr.live_analyze_jobs(scan)
        self.assertEqual({job.status for job in rows}, {JobStatus.CONSUMED})
        self.assertTrue(all(job.consumed_at for job in rows))
        self.upload.assert_called_once()

    def test_the_shard_results_are_kept_in_s3(self):
        """The future smart glue re-reads them (issue #202)."""
        self.build(shard_count=2, pages_per_shard=1)

        with patch("scanning.s3_sync.delete_objects") as delete:
            dots_mocr.finish_ready_runs()

        delete.assert_not_called()

    def test_no_scan_status_is_written(self):
        """The invariant of issue #190: progress lives on the rows.

        The status write of the apply step (#149/#204) belongs to
        ``apply_ready_runs``, not to the glue."""
        scan, _ = self.build(shard_count=2, pages_per_shard=1)
        before = scan.status

        dots_mocr.finish_ready_runs()

        scan.refresh_from_db()
        self.assertEqual(scan.status, before)

    def test_a_run_still_in_flight_is_left_alone(self):
        _, rows = self.build(shard_count=3, pages_per_shard=1)
        ExternalJob.objects.filter(pk=rows[2].pk).update(
            status=JobStatus.SUBMITTED
        )

        glued = dots_mocr.finish_ready_runs()

        self.assertEqual(glued, 0)
        self.download.assert_not_called()

    def test_a_run_with_a_pending_shard_is_left_alone(self):
        _, rows = self.build(shard_count=2, pages_per_shard=1)
        ExternalJob.objects.filter(pk=rows[1].pk).update(
            status=JobStatus.PENDING
        )

        glued = dots_mocr.finish_ready_runs()

        self.assertEqual(glued, 0)
        self.download.assert_not_called()

    def test_a_dead_shard_skips_the_run_silently(self):
        """No scan status to error into; ``run_summary`` shows it, and
        the staff button opens the fresh run."""
        _, rows = self.build(shard_count=2, pages_per_shard=1)
        ExternalJob.objects.filter(pk=rows[1].pk).update(
            status=JobStatus.FAILED, error_code="BAD_INPUT"
        )

        glued = dots_mocr.finish_ready_runs()

        self.assertEqual(glued, 0)
        self.download.assert_not_called()
        self.assertEqual(
            ExternalJob.objects.get(pk=rows[0].pk).status,
            JobStatus.COMPLETED,
        )

    def test_a_consumed_run_is_not_re_glued(self):
        scan, _ = self.build(shard_count=2, pages_per_shard=1)
        ExternalJob.objects.filter(scan=scan).update(status=JobStatus.CONSUMED)

        glued = dots_mocr.finish_ready_runs()

        self.assertEqual(glued, 0)
        self.download.assert_not_called()

    def test_a_mixed_run_is_re_glued(self):
        """A crash between the upload and the CONSUMED write leaves a
        mix; the results are kept, so gluing again is safe."""
        scan, rows = self.build(shard_count=2, pages_per_shard=1)
        ExternalJob.objects.filter(pk=rows[0].pk).update(
            status=JobStatus.CONSUMED
        )

        glued = dots_mocr.finish_ready_runs()

        self.assertEqual(glued, 1)
        rows = dots_mocr.live_analyze_jobs(scan)
        self.assertEqual({job.status for job in rows}, {JobStatus.CONSUMED})

    def test_nothing_happens_while_s3_is_off(self):
        """The glue downloads and uploads, so without S3 there is
        nothing it could correctly do."""
        self.build(shard_count=1, pages_per_shard=1)

        with patch("scanning.s3_sync.s3_active", return_value=False):
            glued = dots_mocr.finish_ready_runs()

        self.assertEqual(glued, 0)
        self.download.assert_not_called()

    def test_a_glue_failure_is_counted_and_retried(self):
        _, rows = self.build(shard_count=1, pages_per_shard=2)
        self.write_envelope(0, make_envelope(rows[0], [make_page(0)]))

        with self.assertLogs("scanning.dots_mocr", level="WARNING"):
            glued = dots_mocr.finish_ready_runs()

        self.assertEqual(glued, 0)
        head = ExternalJob.objects.get(pk=rows[0].pk)
        self.assertEqual(head.status, JobStatus.COMPLETED)
        self.assertEqual(head.provider_meta["glue"]["attempts"], 1)
        self.assertIn("shard 0", head.provider_meta["glue"]["last_error"])

        with self.assertLogs("scanning.dots_mocr", level="WARNING"):
            dots_mocr.finish_ready_runs()

        head.refresh_from_db()
        self.assertEqual(head.provider_meta["glue"]["attempts"], 2)

    def test_the_tries_run_out_loudly_then_quietly(self):
        """One ERROR at the crossing -- the Sentry event -- then skips,
        so a failure that will not change does not repeat every tick."""
        _, rows = self.build(shard_count=1, pages_per_shard=2)
        self.write_envelope(0, make_envelope(rows[0], [make_page(0)]))
        head = ExternalJob.objects.get(pk=rows[0].pk)
        head.provider_meta = {
            "glue": {"attempts": dots_mocr.GLUE_MAX_ATTEMPTS - 1}
        }
        head.save(update_fields=["provider_meta"])

        with self.assertLogs("scanning.dots_mocr", level="ERROR"):
            dots_mocr.finish_ready_runs()

        head.refresh_from_db()
        self.assertEqual(
            head.provider_meta["glue"]["attempts"],
            dots_mocr.GLUE_MAX_ATTEMPTS,
        )
        self.assertEqual(head.status, JobStatus.COMPLETED)

        self.download.reset_mock()
        glued = dots_mocr.finish_ready_runs()

        self.assertEqual(glued, 0)
        self.download.assert_not_called()

    def test_a_failed_upload_is_a_glue_failure(self):
        """S3-only output: a silent no-op would lose the document."""
        self.build(shard_count=1, pages_per_shard=1)
        self.upload.return_value = False

        with self.assertLogs("scanning.dots_mocr", level="WARNING"):
            glued = dots_mocr.finish_ready_runs()

        self.assertEqual(glued, 0)
