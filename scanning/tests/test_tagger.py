"""Tests for the tagger stage (``scanning/tagger.py``): one row per
volume, the input written once by digest, the glue on the collect tick,
and the command that is its only caller.

No HTTP and no S3: ``s3_sync`` is patched at the functions the stage
calls, and the glued OCR document comes from ``test_tagger_input``.
"""

from __future__ import annotations

import json
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from scanning import (
    boundaries,
    jobs,
    redactions,
    runpod_client,
    tagger,
    tagger_input,
)
from scanning.factories import OpinionBoundaryFactory, ScanFactory
from scanning.models import (
    ApplyRun,
    Detection,
    ExternalJob,
    JobEngine,
    JobProvider,
    JobStage,
    JobStatus,
    Redaction,
    Status,
)
from scanning.tests.test_tagger_input import boxes, document
from scanning.tests.test_views import ScanningTestCase

TAGGER = {
    "RUNPOD_ENABLED": True,
    "RUNPOD_API_KEY": "key-1",
    "RUNPOD_PRESIGNED_TTL": 3600,
    "RUNPOD_REQUEST_TIMEOUT": 600,
    "TAGGER_ENABLED": True,
    "RUNPOD_TAGGER_ENDPOINT_ID": "ep-tagger",
    "TAGGER_MAX_CONCURRENCY": 3,
    "TAGGER_MAX_ATTEMPTS": 3,
    "TAGGER_SECONDS_PER_PAGE": 1.0,
    "DOCTOR_ENABLED": False,
}

OCR_KEY = "processing/7/x/1/1/jobs/analyze/dots_mocr/r2-volume.json"


def tag_jobs(scan):
    return list(
        ExternalJob.objects.filter(
            scan=scan, stage=JobStage.TAG, engine=JobEngine.CASELAW_TAGGER
        ).order_by("run", "shard_index")
    )


def make_detections(scan):
    """Write the reviewed boxes of the synthetic volume as rows."""
    for i, b in enumerate(boxes()):
        Detection.objects.create(
            scan=scan,
            page_index=b["page_index"],
            label=b["label"],
            label_id=i,
            confidence=0.9,
            x0=b["x0"],
            y0=b["y0"],
            x1=b["x1"],
            y1=b["y1"],
            img_width=b["img_width"],
            img_height=b["img_height"],
            model_name=Detection.ModelName.BL_WARM,
            found_by=[{"model": "bl_warm", "confidence": 0.9}],
            source_page=b["page_index"] + 1,
            source_fingerprint=scan.source_fingerprint,
        )


class _S3Case(ScanningTestCase):
    """A scan with a glued OCR document and reviewed boxes, and an S3
    that answers without a network."""

    def setUp(self):
        super().setUp()
        self.scan = ScanFactory(status=Status.REDACTION_REVIEW_DONE)
        make_detections(self.scan)
        self.document = document()
        self.stored: dict[str, dict] = {}
        self.enterContext(
            patch("scanning.s3_sync.s3_active", return_value=True)
        )
        self.enterContext(
            patch("scanning.dots_mocr.glued_volume_key", return_value=OCR_KEY)
        )
        self.enterContext(
            patch(
                "scanning.s3_sync.download_json_object",
                side_effect=lambda key: self.stored.get(key, self.document),
            )
        )
        self.enterContext(
            patch(
                "scanning.s3_sync.object_exists",
                side_effect=lambda key: key in self.stored,
            )
        )
        self.upload = self.enterContext(
            patch(
                "scanning.s3_sync.upload_json_object",
                side_effect=self._store,
            )
        )

    def _store(self, key, data):
        self.stored[key] = json.loads(json.dumps(data))
        return True


class TestEnabled(ScanningTestCase):
    @override_settings(**TAGGER)
    def test_on_when_every_switch_is_set(self):
        self.assertTrue(tagger.enabled())

    @override_settings(**{**TAGGER, "TAGGER_ENABLED": False})
    def test_the_stage_switch(self):
        self.assertFalse(tagger.enabled())

    @override_settings(**{**TAGGER, "RUNPOD_TAGGER_ENDPOINT_ID": ""})
    def test_a_blank_endpoint_turns_this_engine_off_alone(self):
        self.assertFalse(tagger.enabled())

    def test_the_engine_table_knows_the_row(self):
        scan = ScanFactory()
        row = ExternalJob(
            scan=scan,
            stage=JobStage.TAG,
            engine=JobEngine.CASELAW_TAGGER,
            provider=JobProvider.RUNPOD,
        )
        spec = jobs._runpod_engine(row)
        self.assertEqual(spec.endpoint_setting, "RUNPOD_TAGGER_ENDPOINT_ID")
        self.assertIs(spec.build_payload, tagger.build_payload)


class TestEnsureTagJobs(_S3Case):
    def test_one_row_per_volume_with_the_input_written_by_digest(self):
        rows = tagger.ensure_tag_jobs(self.scan)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(
            (row.stage, row.engine, row.provider, row.status),
            (
                JobStage.TAG,
                JobEngine.CASELAW_TAGGER,
                JobProvider.RUNPOD,
                JobStatus.PENDING,
            ),
        )
        self.assertEqual(
            (row.shard_index, row.shard_count, row.run), (0, 1, 1)
        )
        # Two objects written, the input and its map, under the stage
        # prefix and named by the text digest.
        self.assertEqual(self.upload.call_count, 2)
        self.assertTrue(row.input_key.endswith(".json"))
        self.assertIn("/jobs/tag/caselaw_tagger/input-", row.input_key)
        self.assertIn(tagger.map_key_for(row.input_key), self.stored)
        stored = self.stored[row.input_key]
        self.assertEqual(len(stored["sequences"]), 2)
        self.assertEqual(
            row.input_manifest["digest"], tagger_input.text_digest(stored)
        )
        # The deadline multiplies by page_count: the pages the sent text
        # covers, since a case may be one page or two hundred.
        self.assertEqual(row.input_manifest["sequence_count"], 2)
        self.assertEqual(row.input_manifest["page_count"], 3)
        self.assertEqual(row.input_manifest["ocr_key"], OCR_KEY)
        self.assertEqual(row.input_manifest["ocr_run"], 2)
        self.assertEqual(
            row.input_manifest["converter"], tagger_input.CONVERTER_VERSION
        )
        self.assertEqual(
            row.source_fingerprint, self.scan.source_fingerprint or ""
        )

    def test_a_second_call_reuses_the_run_and_writes_nothing(self):
        first = tagger.ensure_tag_jobs(self.scan)
        self.upload.reset_mock()
        second = tagger.ensure_tag_jobs(self.scan)
        self.assertEqual([r.pk for r in first], [r.pk for r in second])
        self.upload.assert_not_called()
        self.assertEqual(len(tag_jobs(self.scan)), 1)

    def test_a_changed_input_starts_a_new_run(self):
        tagger.ensure_tag_jobs(self.scan)
        # A curator deactivates the key icon: the cut moves, the text
        # of the sequences changes, the digest changes.
        Detection.objects.filter(scan=self.scan, label="KEY_ICON").update(
            active=False
        )
        rows = tagger.ensure_tag_jobs(self.scan)
        self.assertEqual(rows[0].run, 2)
        self.assertEqual(len(tag_jobs(self.scan)), 2)
        self.assertEqual(len(self.stored[rows[0].input_key]["sequences"]), 1)

    def test_a_dead_row_forces_a_fresh_run(self):
        first = tagger.ensure_tag_jobs(self.scan)
        ExternalJob.objects.filter(pk=first[0].pk).update(
            status=JobStatus.FAILED
        )
        second = tagger.ensure_tag_jobs(self.scan)
        self.assertEqual(second[0].run, 2)

    def test_no_glued_ocr_document_is_refused(self):
        with patch("scanning.dots_mocr.glued_volume_key", return_value=None):
            with self.assertRaisesRegex(tagger.TaggerInputError, "no glued"):
                tagger.ensure_tag_jobs(self.scan)
        self.assertEqual(tag_jobs(self.scan), [])

    def test_no_s3_is_refused(self):
        with patch("scanning.s3_sync.s3_active", return_value=False):
            with self.assertRaisesRegex(tagger.TaggerInputError, "S3"):
                tagger.ensure_tag_jobs(self.scan)

    def test_build_payload(self):
        row = tagger.ensure_tag_jobs(self.scan)[0]
        row.result_key = "jobs/tag/caselaw_tagger/r1-s0-a1.json"
        payload = tagger.build_payload(row, "https://get", "https://put")
        self.assertEqual(
            payload,
            {
                "action": "tag",
                "scan_pk": self.scan.pk,
                "input_url": "https://get",
                "result_url": "https://put",
                "result_key": "jobs/tag/caselaw_tagger/r1-s0-a1.json",
            },
        )

    @override_settings(**TAGGER)
    def test_the_wave_submits_the_row_with_the_payload(self):
        tagger.ensure_tag_jobs(self.scan)
        with (
            patch("scanning.s3_sync.presign_get", return_value="https://get"),
            patch("scanning.s3_sync.presign_put", return_value="https://put"),
            patch(
                "scanning.runpod_client.submit_job", return_value="job-1"
            ) as submit,
        ):
            jobs.submit_pending()
        submit.assert_called_once()
        endpoint, _headers, payload = submit.call_args[0][:3]
        # The client is handed the endpoint's URL, built from its id.
        self.assertTrue(endpoint.endswith("/ep-tagger"), endpoint)
        self.assertEqual(payload["action"], "tag")
        self.assertEqual(payload["input_url"], "https://get")
        row = tag_jobs(self.scan)[0]
        self.assertEqual(row.status, JobStatus.SUBMITTED)
        self.assertEqual(row.external_id, "job-1")


def computed_rect(scan, **fields):
    """Store one computed redaction box, in points."""
    values = {
        "scan": scan,
        "origin": Redaction.Origin.COMPUTED,
        "rect_type": "headnote",
        "fill": "black",
        "x0": 36.0,
        "y0": 72.0,
        "x1": 180.0,
        "y1": 144.0,
        "source_page": 1,
        "source_fingerprint": scan.source_fingerprint,
        "page_index": 0,
    }
    values.update(fields)
    return Redaction.objects.create(**values)


class TestRedactionRects(ScanningTestCase):
    """The blackout geometry comes from the ``Redaction`` rows, in the
    200 dpi frame (#240 PR B)."""

    def test_the_visible_rows_are_read_into_the_frame(self):
        scan = ScanFactory()
        computed_rect(scan)
        computed_rect(
            scan, page_index=2, rect_type="margin", fill="white", y0=0.0
        )
        rects = tagger.redaction_rects(scan)
        # 200/72: 36 pt is 100 px.
        self.assertEqual(rects[0], [(100.0, 200.0, 500.0, 400.0)])
        # A margin strip covers text too, so it is read like a box.
        self.assertEqual(rects[2], [(100.0, 0.0, 500.0, 400.0)])
        self.assertEqual(sorted(rects), [0, 2])

    def test_a_dismissed_box_and_a_dismiss_row_are_not_read(self):
        scan = ScanFactory()
        row = computed_rect(scan)
        redactions.dismiss(scan, row, self.make_user())
        self.assertEqual(tagger.redaction_rects(scan), {})

    def test_a_row_in_another_space_is_left_out(self):
        scan = ScanFactory()
        computed_rect(scan)
        run = ApplyRun.objects.create(scan=scan, number=1, page_map=[])
        with self.assertLogs("scanning.tagger", level="WARNING"):
            self.assertEqual(tagger.redaction_rects(scan, run), {})
        self.assertEqual(len(tagger.redaction_rects(scan)), 1)

    def test_no_rows_excludes_nothing(self):
        self.assertEqual(tagger.redaction_rects(ScanFactory()), {})


class TestOpinionBoundaries(ScanningTestCase):
    """The opinion boundaries come from the ``OpinionBoundary`` rows,
    as anchors in the frame (#240 PR C)."""

    def test_the_standing_rows_become_anchors(self):
        scan = ScanFactory()
        OpinionBoundaryFactory(scan=scan)
        anchors = tagger.opinion_boundaries(scan)
        self.assertEqual(len(anchors), 1)
        start, end = anchors[0]
        self.assertEqual(start[0], 0)
        self.assertAlmostEqual(start[1], 72.0 * 200 / 72)
        self.assertAlmostEqual(start[2], 100.0 * 200 / 72)
        self.assertEqual(end[0], 1)
        self.assertAlmostEqual(end[1], 540.0 * 200 / 72)
        self.assertAlmostEqual(end[2], 700.0 * 200 / 72)

    def test_a_dismissed_boundary_opens_nothing(self):
        scan = ScanFactory()
        row = OpinionBoundaryFactory(scan=scan)
        boundaries.dismiss(scan, row, self.make_user())
        self.assertIsNone(tagger.opinion_boundaries(scan))

    def test_a_row_in_another_space_is_left_out(self):
        scan = ScanFactory()
        OpinionBoundaryFactory(scan=scan)
        run = ApplyRun.objects.create(scan=scan, number=1, page_map=[])
        with self.assertLogs("scanning.tagger", level="WARNING"):
            self.assertIsNone(tagger.opinion_boundaries(scan, run))

    def test_no_rows_is_none_so_the_key_icons_cut(self):
        self.assertIsNone(tagger.opinion_boundaries(ScanFactory()))


class TestPrepareInputSpaces(_S3Case):
    """``prepare_input`` reads every input in the one space the compute
    measured (#269)."""

    def test_the_boundaries_cut_the_volume(self):
        # One reviewed opinion from the caption on page 0 to the figure
        # on page 2, in points (the frame is 200 dpi): the tail of the
        # earlier opinion before the caption is in no boundary and is
        # not sent.
        OpinionBoundaryFactory(
            scan=self.scan,
            start_page_index=0,
            start_x=140 * 72 / 200,
            start_y=510 * 72 / 200,
            end_page_index=2,
            end_x=1500 * 72 / 200,
            end_y=1500 * 72 / 200,
        )
        prepared = tagger.prepare_input(self.scan)
        self.assertEqual(len(prepared.input_document["sequences"]), 1)
        text = prepared.input_document["sequences"][0]["text"]
        self.assertTrue(text.startswith("<p>Jane ROE, Appellant,</p>"))
        self.assertNotIn("The judgment is affirmed.", text)
        self.assertEqual(prepared.stats["blocks_outside_boundaries"], 1)
        self.assertEqual(prepared.map_document["opinions_from"], "boundaries")
        self.assertEqual(prepared.map_document["ocr_key"], OCR_KEY)
        self.assertIsNone(prepared.map_document["apply_run"])

    def test_the_redaction_rows_remove_the_cells(self):
        # The headnote cell on page 0 sits at (900..1550, 400..440) in
        # the frame; the row is stored in points.
        computed_rect(
            self.scan,
            x0=890 * 72 / 200,
            y0=390 * 72 / 200,
            x1=1560 * 72 / 200,
            y1=450 * 72 / 200,
        )
        prepared = tagger.prepare_input(self.scan)
        text = "\n".join(
            s["text"] for s in prepared.input_document["sequences"]
        )
        self.assertNotIn("West headnote", text)
        self.assertEqual(prepared.stats["redacted_cells"], 1)

    def test_a_measured_run_is_read_in_the_final_space(self):
        run = ApplyRun.objects.create(
            scan=self.scan,
            number=1,
            page_map=[],
            ocr_key="processing/7/jobs/apply/a1/ocr-volume.json",
            printed_pages_key="processing/7/jobs/apply/a1/printed_pages.json",
        )
        final_document = json.loads(json.dumps(self.document))
        final_document["run"] = None
        printed = {
            "pages": [
                {"final_page": 1, "printed": "101"},
                {"final_page": 2, "printed": None},
                {"final_page": 3, "printed": "103"},
            ]
        }
        with (
            patch("scanning.detections.measured_run", return_value=run),
            patch(
                "scanning.apply.load_ocr_document", return_value=final_document
            ) as load_ocr,
            patch("scanning.apply.load_printed_pages", return_value=printed),
            patch("scanning.dots_mocr.glued_volume_key") as live_key,
        ):
            prepared = tagger.prepare_input(self.scan)
        load_ocr.assert_called_once_with(self.scan, run)
        live_key.assert_not_called()
        self.assertEqual(prepared.ocr_key, run.ocr_key)
        self.assertIs(prepared.apply_run, run)
        self.assertEqual(prepared.map_document["apply_run"], run.pk)
        self.assertEqual(
            prepared.map_document["sequences"][1]["printed_pages"],
            ["101", "103"],
        )
        with (
            patch("scanning.detections.measured_run", return_value=run),
            patch(
                "scanning.apply.load_ocr_document", return_value=final_document
            ),
            patch("scanning.apply.load_printed_pages", return_value=printed),
        ):
            row = tagger.ensure_tag_jobs(self.scan)[0]
        self.assertEqual(row.input_manifest["ocr_key"], run.ocr_key)
        self.assertEqual(row.input_manifest["apply_run"], run.pk)


class TestGlue(_S3Case):
    def completed_row(self):
        row = tagger.ensure_tag_jobs(self.scan)[0]
        row.result_key = "processing/x/jobs/tag/caselaw_tagger/r1-s0-a1.json"
        row.status = JobStatus.COMPLETED
        row.attempt = 1
        row.save()
        return row

    def envelope(self, row, **overrides):
        env = {
            "schema_version": runpod_client.RESULT_SCHEMA_VERSION,
            "action": "tag",
            "scan_pk": self.scan.pk,
            "result_key": row.result_key,
            "payload": {
                "sequences": [
                    {
                        "id": f"{self.scan.pk}-0001",
                        "token_count": 9,
                        "window_count": 1,
                        "spans": [],
                    },
                    {
                        "id": f"{self.scan.pk}-0002",
                        "token_count": 40,
                        "window_count": 1,
                        "spans": [
                            {
                                "start": 3,
                                "end": 23,
                                "label": "party",
                                "text": "Jane ROE, Appellant,",
                            }
                        ],
                    },
                ],
                "sequence_count": 2,
                "failed_sequences": [],
                "model": "freelawproject/caselaw-block-tagger",
                "max_tokens": 8192,
                "duration_ms": 400,
            },
        }
        env.update(overrides)
        return env

    def test_a_completed_run_is_glued_and_consumed(self):
        row = self.completed_row()
        self.stored[row.result_key] = self.envelope(row)
        glued = tagger.finish_ready_runs()
        self.assertEqual(glued, 1)
        row.refresh_from_db()
        self.assertEqual(row.status, JobStatus.CONSUMED)
        key = tagger.glued_result_key(self.scan, 1)
        volume = self.stored[key]
        self.assertEqual(volume["run"], 1)
        self.assertEqual(volume["input_key"], row.input_key)
        self.assertEqual(volume["map_key"], tagger.map_key_for(row.input_key))
        self.assertEqual(volume["ocr_key"], OCR_KEY)
        self.assertEqual(len(volume["sequences"]), 2)
        self.assertEqual(volume["sequences"][1]["spans"][0]["label"], "party")
        self.assertEqual(tagger.glued_volume_key(self.scan), key)

    def test_a_foreign_envelope_is_refused_and_counted(self):
        row = self.completed_row()
        self.stored[row.result_key] = self.envelope(
            row, scan_pk=self.scan.pk + 1
        )
        self.assertEqual(tagger.finish_ready_runs(), 0)
        row.refresh_from_db()
        self.assertEqual(row.status, JobStatus.COMPLETED)
        self.assertEqual(row.provider_meta["glue"]["attempts"], 1)
        self.assertIn("scan_pk", row.provider_meta["glue"]["last_error"])
        self.assertIsNone(tagger.glued_volume_key(self.scan))

    def test_out_of_tries_is_left_alone(self):
        row = self.completed_row()
        self.stored[row.result_key] = self.envelope(row, action="parse")
        for _ in range(tagger.GLUE_MAX_ATTEMPTS):
            tagger.finish_ready_runs()
        row.refresh_from_db()
        self.assertEqual(
            row.provider_meta["glue"]["attempts"], tagger.GLUE_MAX_ATTEMPTS
        )
        self.stored[row.result_key] = self.envelope(row)
        # Even a good envelope is not read again: recovery is a deploy
        # plus clearing the counter, the dots.mocr glue's rule.
        self.assertEqual(tagger.finish_ready_runs(), 0)

    def test_a_pending_row_is_not_glued(self):
        tagger.ensure_tag_jobs(self.scan)
        self.assertEqual(tagger.finish_ready_runs(), 0)


class TestCommand(_S3Case):
    def run_command(self, *args):
        out = StringIO()
        call_command("enqueue_caselaw_tagger", *args, stdout=out)
        return out.getvalue()

    def test_names_are_required(self):
        with self.assertRaisesRegex(CommandError, "Name the scans"):
            self.run_command()

    @override_settings(**TAGGER)
    def test_a_named_volume_past_review_2_gets_one_run(self):
        out = self.run_command(str(self.scan.pk))
        self.assertIn(f"scan {self.scan.pk}: run 1, 2 opinions", out)
        self.assertEqual(len(tag_jobs(self.scan)), 1)

    @override_settings(**TAGGER)
    def test_a_volume_still_in_review_is_refused_without_the_flag(self):
        self.scan.status = Status.READY_FOR_REDACTION_REVIEW
        self.scan.save(update_fields=["status"])
        with self.assertRaisesRegex(CommandError, "Not past review 2"):
            self.run_command(str(self.scan.pk))
        self.assertEqual(tag_jobs(self.scan), [])
        out = self.run_command(str(self.scan.pk), "--any-status")
        self.assertIn("run 1", out)

    @override_settings(**TAGGER)
    def test_dry_run_builds_the_input_and_writes_nothing(self):
        out = self.run_command(str(self.scan.pk), "--dry-run")
        self.assertIn("would send 2 opinions", out)
        self.upload.assert_not_called()
        self.assertEqual(tag_jobs(self.scan), [])

    @override_settings(**{**TAGGER, "TAGGER_ENABLED": False})
    def test_a_disabled_stage_refuses_to_create_rows(self):
        with self.assertRaisesRegex(CommandError, "not enabled"):
            self.run_command(str(self.scan.pk))

    @override_settings(**TAGGER)
    def test_pending_takes_every_finished_volume_without_a_run(self):
        other = ScanFactory(status=Status.REDACTION_REVIEW_DONE)
        make_detections(other)
        ScanFactory(
            status=Status.READY_FOR_REDACTION_REVIEW
        )  # still in review
        out = self.run_command("--pending")
        self.assertIn("2 run(s) live", out)
        self.assertEqual(len(tag_jobs(other)), 1)
        # A second pass finds nothing left.
        self.assertIn("Nothing to tag", self.run_command("--pending"))
