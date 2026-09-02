"""Tests for the detection merge (issue #196).

S3 is inert under ``TESTING``, so the merge's downloads are patched to
copy envelopes from a local directory, and its upload is captured.
Under test:

- the page arithmetic: shard-local ``page_index`` to volume
  ``page_index``
- the checks that stand between a paid result and a document merged
  from the wrong bytes
- ``found_by``, which decides the confidence gates downstream
- the finish pass: rows consumed, results kept, no scan status
  written, and a bounded retry of a merge that keeps failing
"""

import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from scanning import s3_sync, yolo
from scanning.factories import ScanFactory
from scanning.models import ExternalJob, JobStatus
from scanning.tests.test_jobs import make_manifest


def make_detection(page_index: int, label: str = "PAGE_HEADER") -> dict:
    """Build one detection of the shape the worker reports.

    :param page_index: 0-based page index inside the shard.
    :param label: The detection's label.
    :returns: A detection dict.
    :rtype: dict
    """
    return {
        "page_index": page_index,
        "label": label,
        "label_id": 2,
        "confidence": 0.92,
        "bbox": [673.8, 92.0, 1087.8, 151.5],
        "img_width": 1700,
        "img_height": 2200,
        "found_by": [{"model": "bl_warm", "confidence": 0.92}],
        "model_count": 1,
    }


def make_envelope(
    job: ExternalJob, detections: list[dict], page_count: int, **overrides
) -> dict:
    """Build the result envelope one worker attempt PUTs to S3.

    :param job: The row the envelope answers.
    :param detections: The payload's detections.
    :param page_count: Pages the shard held, as the worker saw it.
    :param overrides: Envelope fields to replace, for the check tests.
    :returns: An envelope dict.
    :rtype: dict
    """
    payload = {
        "detections": detections,
        "page_count": page_count,
        "models": ["bl_warm"],
        "duration_ms": 1000,
    }
    payload.update(overrides.pop("payload", {}))
    envelope = {
        "schema_version": 1,
        "action": "detect",
        "scan_pk": job.scan_id,
        "result_key": job.result_key,
        "payload": payload,
    }
    envelope.update(overrides)
    return envelope


class DetectJobsMixin:
    """Builds a scan whose detection jobs have results on 'S3'."""

    def build(self, shard_count=3, pages_per_shard=2, per_page=1):
        """Create a scan, its jobs, and an envelope per shard.

        The envelopes live in a local directory that a patched
        ``download_object`` copies from, so the merge exercises its
        real code path without S3. A test that needs a broken envelope
        rewrites the shard's file through :meth:`write_envelope`.

        :param shard_count: Shards to create.
        :param pages_per_shard: Pages each shard covers.
        :param per_page: Detections on each page.
        :returns: ``(scan, jobs)``.
        """
        self.store = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.store, True)

        scan = ScanFactory(page_count=shard_count * pages_per_shard)
        rows = yolo.ensure_detect_jobs(
            scan, make_manifest(shard_count, pages_per_shard)
        )
        for index, job in enumerate(rows):
            job.status = JobStatus.COMPLETED
            job.result_key = f"jobs/detect/blackletter/r1-s{index}-a1.json"
            job.save()
            self.write_envelope(
                index,
                make_envelope(
                    job,
                    [
                        make_detection(page)
                        for page in range(pages_per_shard)
                        for _ in range(per_page)
                    ],
                    pages_per_shard,
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
        return scan, yolo.live_detect_jobs(scan)

    def write_envelope(self, index: int, envelope: dict) -> None:
        """Put ``envelope`` where shard ``index``'s download reads from.

        :param index: The shard index to answer for.
        :param envelope: The envelope to store.
        :return: None.
        """
        (self.store / f"s{index}.json").write_text(json.dumps(envelope))


class TestMergeDetectResults(DetectJobsMixin, TestCase):
    """Merging the shard payloads."""

    def test_every_page_index_is_offset_by_its_shard(self):
        scan, rows = self.build(shard_count=3, pages_per_shard=2)

        yolo.merge_detect_results(scan, rows)

        document = self.upload.call_args[0][1]
        self.assertEqual(
            [row["page_index"] for row in document["detections"]],
            [0, 1, 2, 3, 4, 5],
        )
        self.assertEqual(
            [row["pdf_page"] for row in document["detections"]],
            [1, 2, 3, 4, 5, 6],
        )
        self.assertEqual(
            [row["shard_index"] for row in document["detections"]],
            [0, 0, 1, 1, 2, 2],
        )

    def test_the_document_names_the_run_and_the_volume(self):
        scan, rows = self.build(shard_count=2, pages_per_shard=2)

        yolo.merge_detect_results(scan, rows)

        document = self.upload.call_args[0][1]
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["engine"], "blackletter")
        self.assertEqual(document["action"], "detect")
        self.assertEqual(document["scan_pk"], scan.pk)
        self.assertEqual(document["run"], 1)
        self.assertEqual(document["source_page_count"], 4)
        self.assertEqual(document["dpi"], yolo.DPI)
        self.assertEqual(document["models"], ["bl_warm"])
        self.assertEqual(document["pages_with_detections"], 4)

    def test_the_document_carries_the_originals_fingerprint(self):
        """The apply refuses a document cut from other bytes."""
        scan, rows = self.build(shard_count=1, pages_per_shard=1)
        scan.source_fingerprint = "abc123"
        scan.save(update_fields=["source_fingerprint"])

        yolo.merge_detect_results(scan, rows)

        self.assertEqual(
            self.upload.call_args[0][1]["source_fingerprint"], "abc123"
        )

    def test_the_document_lands_at_a_run_scoped_key(self):
        scan, rows = self.build(shard_count=2, pages_per_shard=1)

        key = yolo.merge_detect_results(scan, rows)

        self.assertEqual(
            key,
            f"{s3_sync.s3_processing_prefix(scan)}"
            f"jobs/detect/blackletter/r1-volume.json",
        )

    def test_the_model_provenance_survives_whole(self):
        """``found_by`` picks the confidence gates (blackletter #73), so
        a merge that dropped it would send the volume back to the
        legacy ones."""
        scan, rows = self.build(shard_count=1, pages_per_shard=1)

        yolo.merge_detect_results(scan, rows)

        detection = self.upload.call_args[0][1]["detections"][0]
        self.assertEqual(
            detection["found_by"], [{"model": "bl_warm", "confidence": 0.92}]
        )
        self.assertEqual(detection["model_count"], 1)
        self.assertEqual(detection["img_width"], 1700)

    def test_shard_provenance_is_recorded_for_the_smart_merge(self):
        scan, rows = self.build(shard_count=2, pages_per_shard=3)

        yolo.merge_detect_results(scan, rows)

        shards = self.upload.call_args[0][1]["shards"]
        self.assertEqual([entry["from_page"] for entry in shards], [0, 3])
        self.assertEqual(
            [entry["result_key"] for entry in shards],
            [job.result_key for job in rows],
        )

    def test_a_page_with_no_detection_is_no_error(self):
        """Unlike the dots.mocr payload, this one lists detections, so a
        page with none is indistinguishable from a page nobody read."""
        scan, rows = self.build(shard_count=1, pages_per_shard=3)
        self.write_envelope(0, make_envelope(rows[0], [make_detection(2)], 3))

        yolo.merge_detect_results(scan, rows)

        document = self.upload.call_args[0][1]
        self.assertEqual(len(document["detections"]), 1)
        self.assertEqual(document["pages_with_detections"], 1)

    def test_merging_twice_gives_the_same_document(self):
        """Idempotent, so a crash before the CONSUMED write costs
        nothing."""
        scan, rows = self.build(shard_count=2, pages_per_shard=2)

        yolo.merge_detect_results(scan, rows)
        first = self.upload.call_args[0][1]
        yolo.merge_detect_results(scan, rows)
        second = self.upload.call_args[0][1]

        first.pop("generated_at")
        second.pop("generated_at")
        self.assertEqual(first, second)

    def test_a_shard_that_read_the_wrong_page_count_is_refused(self):
        scan, rows = self.build(shard_count=2, pages_per_shard=2)
        self.write_envelope(1, make_envelope(rows[1], [], 1))

        with self.assertRaises(yolo.DetectMergeError) as caught:
            yolo.merge_detect_results(scan, rows)

        self.assertIn("shard 1", str(caught.exception))
        self.upload.assert_not_called()

    def test_a_detection_outside_its_shard_is_refused(self):
        scan, rows = self.build(shard_count=2, pages_per_shard=2)
        self.write_envelope(0, make_envelope(rows[0], [make_detection(7)], 2))

        with self.assertRaises(yolo.DetectMergeError) as caught:
            yolo.merge_detect_results(scan, rows)

        self.assertIn("page 7", str(caught.exception))

    def test_shards_that_ran_different_models_are_refused(self):
        """One volume, one model family: the gates are per family."""
        scan, rows = self.build(shard_count=2, pages_per_shard=1)
        self.write_envelope(
            1,
            make_envelope(
                rows[1], [make_detection(0)], 1, payload={"models": ["large"]}
            ),
        )

        with self.assertRaises(yolo.DetectMergeError) as caught:
            yolo.merge_detect_results(scan, rows)

        self.assertIn("large", str(caught.exception))

    def test_a_gap_in_the_shard_sequence_is_refused(self):
        scan, _ = self.build(shard_count=3, pages_per_shard=2)
        rows = yolo.live_detect_jobs(scan)
        ExternalJob.objects.filter(pk=rows[1].pk).delete()

        with self.assertRaises(yolo.DetectMergeError):
            yolo.merge_detect_results(scan, yolo.live_detect_jobs(scan))

    def test_a_row_with_no_result_key_is_refused(self):
        scan, rows = self.build(shard_count=2, pages_per_shard=2)
        ExternalJob.objects.filter(pk=rows[1].pk).update(result_key="")

        with self.assertRaises(yolo.DetectMergeError):
            yolo.merge_detect_results(scan, yolo.live_detect_jobs(scan))

    def test_an_envelope_for_another_scan_is_refused(self):
        scan, rows = self.build(shard_count=1, pages_per_shard=1)
        self.write_envelope(
            0,
            make_envelope(
                rows[0], [make_detection(0)], 1, scan_pk=scan.pk + 1
            ),
        )

        with self.assertRaises(yolo.DetectMergeError) as caught:
            yolo.merge_detect_results(scan, rows)

        self.assertIn("scan_pk", str(caught.exception))

    def test_an_envelope_from_another_action_is_refused(self):
        scan, rows = self.build(shard_count=1, pages_per_shard=1)
        self.write_envelope(
            0,
            make_envelope(rows[0], [make_detection(0)], 1, action="parse"),
        )

        with self.assertRaises(yolo.DetectMergeError) as caught:
            yolo.merge_detect_results(scan, rows)

        self.assertIn("action", str(caught.exception))

    def test_an_unknown_result_schema_is_refused_naming_both(self):
        """A worker deployed ahead of the daemon must read as that, not
        as a bad volume."""
        scan, rows = self.build(shard_count=1, pages_per_shard=1)
        self.write_envelope(
            0,
            make_envelope(rows[0], [make_detection(0)], 1, schema_version=2),
        )

        with self.assertRaises(yolo.DetectMergeError) as caught:
            yolo.merge_detect_results(scan, rows)

        self.assertIn("schema 2", str(caught.exception))
        self.assertIn("knows 1", str(caught.exception))

    def test_a_volume_short_of_the_original_page_count_is_refused(self):
        scan, rows = self.build(shard_count=2, pages_per_shard=2)
        for job in rows:
            job.input_manifest["source_page_count"] = 99
            job.save(update_fields=["input_manifest"])

        with self.assertRaises(yolo.DetectMergeError) as caught:
            yolo.merge_detect_results(scan, yolo.live_detect_jobs(scan))

        self.assertIn("99", str(caught.exception))

    def test_no_jobs_is_an_error_not_an_empty_document(self):
        scan = ScanFactory()

        with self.assertRaises(yolo.DetectMergeError):
            yolo.merge_detect_results(scan, [])


class TestFinishReadyRuns(DetectJobsMixin, TestCase):
    """The daemon pass that merges finished runs."""

    def test_a_completed_run_is_merged_and_consumed(self):
        scan, _ = self.build(shard_count=2, pages_per_shard=2)

        merged = yolo.finish_ready_runs()

        self.assertEqual(merged, 1)
        rows = yolo.live_detect_jobs(scan)
        self.assertEqual({job.status for job in rows}, {JobStatus.CONSUMED})
        self.assertTrue(all(job.consumed_at for job in rows))
        self.upload.assert_called_once()

    def test_the_shard_results_are_kept_in_s3(self):
        """Issue #196 asks for this: a page insert recomputes the merge
        from them."""
        self.build(shard_count=2, pages_per_shard=1)

        with patch("scanning.s3_sync.delete_objects") as delete:
            yolo.finish_ready_runs()

        delete.assert_not_called()

    def test_no_scan_status_is_written(self):
        """The invariant of #195: progress lives on the rows. The status
        write belongs to :func:`yolo.queue_ready_runs`."""
        scan, _ = self.build(shard_count=2, pages_per_shard=1)
        before = scan.status

        yolo.finish_ready_runs()

        scan.refresh_from_db()
        self.assertEqual(scan.status, before)

    def test_a_run_still_in_flight_is_left_alone(self):
        _, rows = self.build(shard_count=3, pages_per_shard=1)
        ExternalJob.objects.filter(pk=rows[2].pk).update(
            status=JobStatus.SUBMITTED
        )

        merged = yolo.finish_ready_runs()

        self.assertEqual(merged, 0)
        self.download.assert_not_called()

    def test_a_dead_shard_skips_the_run_silently(self):
        """No scan status to error into; ``run_summary`` shows it, and
        the staff button opens the fresh run."""
        _, rows = self.build(shard_count=2, pages_per_shard=1)
        ExternalJob.objects.filter(pk=rows[1].pk).update(
            status=JobStatus.FAILED, error_code="BAD_INPUT"
        )

        merged = yolo.finish_ready_runs()

        self.assertEqual(merged, 0)
        self.download.assert_not_called()

    def test_a_consumed_run_is_not_merged_again(self):
        scan, _ = self.build(shard_count=2, pages_per_shard=1)
        ExternalJob.objects.filter(scan=scan).update(status=JobStatus.CONSUMED)

        merged = yolo.finish_ready_runs()

        self.assertEqual(merged, 0)
        self.download.assert_not_called()

    def test_a_mixed_run_is_merged_again(self):
        """A crash between the upload and the CONSUMED write leaves a
        mix; the results are kept, so merging again is safe."""
        scan, rows = self.build(shard_count=2, pages_per_shard=1)
        ExternalJob.objects.filter(pk=rows[0].pk).update(
            status=JobStatus.CONSUMED
        )

        merged = yolo.finish_ready_runs()

        self.assertEqual(merged, 1)
        rows = yolo.live_detect_jobs(scan)
        self.assertEqual({job.status for job in rows}, {JobStatus.CONSUMED})

    def test_nothing_happens_while_s3_is_off(self):
        self.build(shard_count=1, pages_per_shard=1)

        with patch("scanning.s3_sync.s3_active", return_value=False):
            merged = yolo.finish_ready_runs()

        self.assertEqual(merged, 0)
        self.download.assert_not_called()

    def test_a_merge_failure_is_counted_and_bounded(self):
        _, rows = self.build(shard_count=1, pages_per_shard=2)
        self.write_envelope(0, make_envelope(rows[0], [], 1))

        with self.assertLogs("scanning.yolo", level="WARNING"):
            merged = yolo.finish_ready_runs()

        self.assertEqual(merged, 0)
        head = ExternalJob.objects.get(pk=rows[0].pk)
        self.assertEqual(head.status, JobStatus.COMPLETED)
        self.assertEqual(head.provider_meta["merge"]["attempts"], 1)
        self.assertIn("shard 0", head.provider_meta["merge"]["last_error"])

        # Loud until the tries run out, then silent: the pass runs
        # every tick, and a failure that will not change must not fill
        # Sentry with copies of itself.
        for _ in range(yolo.MERGE_MAX_ATTEMPTS - 1):
            with self.assertLogs("scanning.yolo", level="WARNING"):
                yolo.finish_ready_runs()

        head.refresh_from_db()
        self.assertEqual(
            head.provider_meta["merge"]["attempts"], yolo.MERGE_MAX_ATTEMPTS
        )
        self.assertEqual(head.status, JobStatus.COMPLETED)

        with self.assertNoLogs("scanning.yolo", level="WARNING"):
            self.assertEqual(yolo.finish_ready_runs(), 0)
