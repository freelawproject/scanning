"""Tests for the glue phase of the page edit apply (issue #224).

Three glues over one offset map: the final bitonal copy, the final OCR
volume with its printed-page map, and the final detections. Each is
judged on its own inputs. S3 is stood in for by a dict of objects, so
the tests read what would be written and never the bucket.
"""

import json
import pathlib
from unittest.mock import patch

import fitz
from django.test import override_settings

from scanning import apply, dots_mocr, yolo
from scanning.models import (
    ExternalJob,
    JobStage,
    JobStatus,
    PageEdit,
    QueuedAction,
    Scan,
    Status,
)
from scanning.runpod_client import RESULT_SCHEMA_VERSION
from scanning.tests.test_apply import MEDIA_ROOT
from scanning.tests.test_apply_build import BuildTestCase
from scanning.tests.test_jobs import make_manifest


def cell(text: str) -> dict:
    """Return one page-header cell that reads as a printed number.

    :param text: The number.
    :returns: The cell.
    """
    return {"category": "Page-header", "text": text, "bbox": [50, 20, 90, 40]}


def envelope(scan, row, action, payload) -> dict:
    """Wrap a payload the way the workers do.

    :param scan: The scan.
    :param row: The job row the result belongs to.
    :param action: The handler action.
    :param payload: The payload.
    :returns: The envelope.
    """
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "action": action,
        "scan_pk": scan.pk,
        "result_key": row.result_key,
        "payload": payload,
    }


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class GlueTestCase(BuildTestCase):
    """A built run whose rows are complete, over a fake bucket."""

    def setUp(self):
        super().setUp()
        self.objects: dict[str, object] = {}

        def download_json(key):
            if key not in self.objects:
                raise KeyError(key)
            return json.loads(json.dumps(self.objects[key]))

        def download_object(key, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(self.objects[key])

        def upload_json(key, data):
            self.uploads[key] = data
            self.objects[key] = data
            return True

        def upload_file(key, path, content_type):
            self.uploads[key] = path.stat().st_size
            self.objects[key] = path.read_bytes()
            self.present.add(key)
            return True

        for target, side in (
            ("scanning.s3_sync.download_json_object", download_json),
            ("scanning.s3_sync.download_object", download_object),
            ("scanning.s3_sync.upload_json_object", upload_json),
            ("scanning.s3_sync.upload_file_object", upload_file),
            (
                "scanning.s3_sync.object_exists",
                lambda key: key in self.present or key in self.objects,
            ),
        ):
            patcher = patch(target, side_effect=side)
            patcher.start()
            self.addCleanup(patcher.stop)

        # One prefix for every key, so the tests can name them.
        self.prefix = f"processing/{self.scan.pk}/a/{self.scan.volume}/1/"
        prefix = patch(
            "scanning.s3_sync.s3_processing_prefix", return_value=self.prefix
        )
        prefix.start()
        self.addCleanup(prefix.stop)
        # The review-1 bitonal copy: the original's pages, as is.
        self.volume_bitonal_key = f"{self.prefix}bitonal.pdf"
        self.objects[self.volume_bitonal_key] = pathlib.Path(
            self.original
        ).read_bytes()

    def volume_ocr_run(self):
        """Glue a volume OCR run that reads page p as printed p.

        :returns: The glued document's key.
        """
        rows = dots_mocr.ensure_analyze_jobs(
            self.scan, make_manifest(1, self.PAGES)
        )
        ExternalJob.objects.filter(pk__in=[r.pk for r in rows]).update(
            status=JobStatus.CONSUMED
        )
        key = dots_mocr.glued_result_key(self.scan, rows[0].run)
        self.objects[key] = {
            "schema_version": dots_mocr.GLUE_SCHEMA_VERSION,
            "engine": "dots_mocr",
            "action": dots_mocr.ACTION,
            "scan_pk": self.scan.pk,
            "run": rows[0].run,
            "source_page_count": self.PAGES,
            "dpi": 200,
            "prompt_mode": dots_mocr.PROMPT_MODE,
            "pages": [
                {
                    "page_index": p - 1,
                    "pdf_page": p,
                    "shard_index": 0,
                    "origin_width": 1700,
                    "origin_height": 2200,
                    "cells": [cell(str(p))],
                    "md": str(p),
                }
                for p in range(1, self.PAGES + 1)
            ],
            "failed_pages": [],
            "filtered_pages": [],
            "recovered_pages": [],
        }
        return key

    def volume_detect_run(self):
        """Merge a volume detection run with one box on every page.

        :returns: The merged document's key.
        """
        rows = yolo.ensure_detect_jobs(self.scan, make_manifest(1, self.PAGES))
        ExternalJob.objects.filter(pk__in=[r.pk for r in rows]).update(
            status=JobStatus.CONSUMED
        )
        key = yolo.merged_result_key(self.scan, rows[0].run)
        self.objects[key] = {
            "schema_version": yolo.MERGE_SCHEMA_VERSION,
            "engine": "blackletter",
            "action": yolo.ACTION,
            "scan_pk": self.scan.pk,
            "run": rows[0].run,
            "source_page_count": self.PAGES,
            "source_fingerprint": self.scan.source_fingerprint,
            "dpi": 200,
            "models": ["bl_warm"],
            "confidence": 0.2,
            "detections": [
                {
                    "page_index": p - 1,
                    "pdf_page": p,
                    "shard_index": 0,
                    "label": "PAGE_HEADER",
                    "confidence": 0.9,
                    "bbox": [1, 2, 3, 4],
                    "found_by": [{"model": "bl_warm", "confidence": 0.9}],
                }
                for p in range(1, self.PAGES + 1)
            ],
            "pages_with_detections": self.PAGES,
        }
        return key

    def complete_rows(self, run):
        """Answer every apply row with a result of the right shape.

        A converted shard is the shard itself. A read answers every
        page of a shard as printed ``900 + edit pk``; a detection puts
        one box on each page of the shard.

        :param run: The built run.
        """
        for row in run.jobs.all():
            key = f"result/{row.stage}/{row.pk}"
            ExternalJob.objects.filter(pk=row.pk).update(
                status=JobStatus.COMPLETED, result_key=key
            )
            row.result_key = key
            edit_id = row.input_manifest["edit_id"]
            count = row.input_manifest["page_count"]
            if row.stage == JobStage.CONVERT:
                self.objects[key] = self.objects[
                    apply.page_shard_key(
                        self.scan, PageEdit.objects.get(pk=edit_id)
                    )
                ]
            elif row.stage == JobStage.ANALYZE:
                self.objects[key] = envelope(
                    self.scan,
                    row,
                    dots_mocr.ACTION,
                    {
                        "pages": [
                            {
                                "page_no": k,
                                "origin_width": 1700,
                                "origin_height": 2200,
                                "cells": [cell(f"{900 + edit_id}")],
                                "md": "x",
                                "raw": "raw text",
                            }
                            for k in range(count)
                        ]
                    },
                )
            else:
                self.objects[key] = envelope(
                    self.scan,
                    row,
                    yolo.ACTION,
                    {
                        "page_count": count,
                        "models": ["bl_warm"],
                        "detections": [
                            {
                                "page_index": k,
                                "label": "CASE_CAPTION",
                                "confidence": 0.8,
                                "bbox": [5, 6, 7, 8],
                                "found_by": [
                                    {"model": "bl_warm", "confidence": 0.8}
                                ],
                            }
                            for k in range(count)
                        ],
                    },
                )

    def built_run(self):
        """Build a run with the three edit kinds and complete its rows.

        :returns: ``(run, (turn, swap, leaf))``.
        """
        edits = self.three_edits()
        run = apply.build_run(self.scan)
        self.complete_rows(run)
        return run, edits


class TestGlueDue(GlueTestCase):
    """Each glue waits for its own inputs and no other."""

    def test_the_bitonal_glue_waits_for_nobody_else(self):
        run, _ = self.built_run()
        self.assertEqual(apply.phase_due(self.scan), "glue")

    def test_open_rows_hold_every_glue(self):
        run, _ = self.built_run()
        row = run.jobs.first()
        ExternalJob.objects.filter(pk=row.pk).update(status=JobStatus.IN_QUEUE)
        self.assertIsNone(apply.phase_due(self.scan))

    def test_the_ocr_and_detection_glues_wait_for_the_volume_runs(self):
        run, _ = self.built_run()
        apply.glue_run(self.scan)
        run.refresh_from_db()
        self.assertTrue(run.bitonal_key)
        self.assertEqual(run.ocr_key, "")
        self.assertEqual(run.detections_key, "")
        self.assertIsNone(apply.phase_due(self.scan))

        self.volume_ocr_run()
        self.assertEqual(apply.phase_due(self.scan), "glue")


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestGlues(GlueTestCase):
    """What each glue writes."""

    def test_a_volume_with_no_edit_aliases_everything_and_writes_the_pages(
        self,
    ):
        self.edit(PageEdit.Kind.SET_NUMBER, pdf_page=2, value="12")
        ocr_key = self.volume_ocr_run()
        detect_key = self.volume_detect_run()
        run = apply.build_run(self.scan)

        apply.glue_run(self.scan)

        run.refresh_from_db()
        self.assertEqual(run.bitonal_key, self.volume_bitonal_key)
        self.assertEqual(run.ocr_key, ocr_key)
        self.assertEqual(run.detections_key, detect_key)
        printed = self.objects[run.printed_pages_key]
        self.assertEqual(
            [(p["printed"], p["by"]) for p in printed["pages"]][:3],
            [("1", "model"), ("12", "curator"), ("3", "model")],
        )
        self.assertTrue(run.is_glued)
        self.assertIsNone(apply.phase_due(self.scan))

    def test_the_bitonal_glue_splices_kept_and_new_pages(self):
        run, (turn, swap, leaf) = self.built_run()

        apply.glue_run(self.scan)

        run.refresh_from_db()
        self.assertEqual(
            run.bitonal_key, f"{apply.run_prefix(self.scan, run)}bitonal.pdf"
        )
        with fitz.open(stream=self.objects[run.bitonal_key]) as doc:
            # 6 pages, page 2 deleted, two inserted after page 5.
            self.assertEqual(doc.page_count, 7)
            self.assertEqual(doc[1].rotation, 90)
            self.assertEqual(round(doc[4].rect.width), 300)
        # The one-page results are consumed and kept.
        self.assertEqual(
            set(
                run.jobs.filter(stage=JobStage.CONVERT).values_list(
                    "status", flat=True
                )
            ),
            {JobStatus.CONSUMED},
        )
        self.assertEqual(
            set(
                run.jobs.filter(stage=JobStage.ANALYZE).values_list(
                    "status", flat=True
                )
            ),
            {JobStatus.COMPLETED},
        )

    def test_the_ocr_glue_renumbers_and_keeps_a_hole(self):
        self.volume_ocr_run()
        run, (turn, swap, leaf) = self.built_run()
        # The read of the inserted leaf failed on its second page.
        row = run.jobs.get(
            stage=JobStage.ANALYZE, input_manifest__edit_id=leaf.pk
        )
        self.objects[row.result_key]["payload"]["pages"][1] = {
            "page_no": 1,
            "cells": [],
            "md": "",
            "error": "loop",
            "raw": "aaaa",
        }
        self.edit(PageEdit.Kind.SET_NUMBER, pdf_page=4, value="44")
        PageEdit.objects.filter(pk=leaf.pk).update(logical_page="5a")

        apply.glue_run(self.scan)

        run.refresh_from_db()
        document = self.objects[run.ocr_key]
        self.assertEqual(document["source_page_count"], 7)
        self.assertEqual(
            [p["pdf_page"] for p in document["pages"]], list(range(1, 8))
        )
        self.assertEqual(
            document["pages"][0]["source"], {"kind": "original", "pdf_page": 1}
        )
        self.assertEqual(document["pages"][1]["source"]["edit_id"], turn.pk)
        self.assertEqual(document["failed_pages"], [5])
        self.assertNotIn("raw", document["pages"][4])

        printed = self.objects[run.printed_pages_key]
        by_final = {p["final_page"]: p for p in printed["pages"]}
        # Page 1 kept its read; the rotated page 3 stands at final 2
        # with the read of its own shard; the replaced page 4 at final
        # 3 carries the curator's number; the inserted leaf carries its
        # label on both pages; page 6 kept its read at final 7.
        self.assertEqual(by_final[1]["printed"], "1")
        self.assertEqual(by_final[2]["printed"], str(900 + turn.pk))
        self.assertEqual(
            (by_final[3]["printed"], by_final[3]["by"]), ("44", "curator")
        )
        self.assertEqual(
            (by_final[5]["printed"], by_final[5]["by"]), ("5a", "curator")
        )
        self.assertEqual(by_final[6]["printed"], "5a")
        self.assertEqual(by_final[7]["printed"], "6")

    def test_the_detections_glue_drops_changed_pages_and_adds_new_ones(self):
        self.volume_detect_run()
        run, (turn, swap, leaf) = self.built_run()

        apply.glue_run(self.scan)

        run.refresh_from_db()
        document = self.objects[run.detections_key]
        self.assertEqual(document["source_page_count"], 7)
        self.assertEqual(document["source_fingerprint"], "100:6")
        by_final: dict[int, list] = {}
        for det in document["detections"]:
            by_final.setdefault(det["pdf_page"], []).append(det)
        # Kept pages 1, 5, 6 keep the volume's header box.
        self.assertEqual(by_final[1][0]["label"], "PAGE_HEADER")
        self.assertEqual(by_final[4][0]["label"], "PAGE_HEADER")
        self.assertEqual(by_final[7][0]["label"], "PAGE_HEADER")
        # The rotated, replaced and inserted pages carry their shard's.
        for final in (2, 3, 5, 6):
            self.assertEqual(by_final[final][0]["label"], "CASE_CAPTION")
        self.assertEqual(by_final[2][0]["source"]["edit_id"], turn.pk)
        self.assertEqual(document["pages_with_detections"], 7)
        self.assertEqual(
            run.jobs.filter(
                stage=JobStage.DETECT, status=JobStatus.CONSUMED
            ).count(),
            3,
        )

    def test_a_detection_family_mismatch_is_refused(self):
        self.volume_detect_run()
        run, (turn, swap, leaf) = self.built_run()
        row = run.jobs.filter(stage=JobStage.DETECT).first()
        self.objects[row.result_key]["payload"]["models"] = ["large"]

        with self.assertRaises(apply.ApplyError):
            apply.glue_run(self.scan)

        run.refresh_from_db()
        self.assertEqual(run.attempts, 1)
        self.assertTrue(run.bitonal_key)
        self.assertEqual(run.detections_key, "")

    def test_a_failed_glue_is_retried_next_tick(self):
        run, _ = self.built_run()
        with patch("scanning.s3_sync.upload_file_object", return_value=False):
            with self.assertRaises(apply.ApplyError) as caught:
                apply.glue_run(self.scan)
        self.assertIn("runs again by itself", str(caught.exception))
        self.assertEqual(apply.phase_due(self.scan), "glue")

        apply.glue_run(self.scan)
        run.refresh_from_db()
        self.assertTrue(run.bitonal_key)
        self.assertEqual(run.attempts, 0)


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestRedactionTriggerGate(GlueTestCase):
    """The redaction compute waits for the apply's outputs (#196 gate)."""

    def test_a_merged_detection_run_waits_for_the_apply(self):
        self.volume_detect_run()

        self.assertEqual(yolo.queue_ready_runs(), 0)

        # The apply glues on the next claim; then the redactions queue.
        apply.build_run(self.scan)
        apply.glue_run(self.scan)
        self.assertEqual(yolo.queue_ready_runs(), 1)
        self.scan.refresh_from_db()
        self.assertEqual(
            self.scan.queued_action, QueuedAction.COMPUTE_REDACTIONS
        )

    def test_the_worker_runs_both_phases_for_a_volume_with_no_edit(self):
        from scanning import services

        self.volume_ocr_run()
        Scan.objects.filter(pk=self.scan.pk).update(status=Status.PROCESSING)

        services.run_apply_page_edits(self.scan.pk)

        run = apply.current_run(self.scan)
        self.assertTrue(run.is_built)
        self.assertTrue(run.is_glued)
        self.scan.refresh_from_db()
        self.assertEqual(
            self.scan.status, Status.PAGE_COMPLETENESS_REVIEW_DONE
        )
        self.assertIn("detections pending", self.scan.progress_message)
