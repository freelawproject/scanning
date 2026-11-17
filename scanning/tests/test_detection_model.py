"""Tests for the detection run read with another weight (#633).

The Hein volumes are bitonal, and ``bl_hein`` is the weight trained on
their pages. Pinned here:

- the weight is part of a row's identity only when it is not the
  default, so no run from before the change stops describing its shard
  set, and a result read with one weight is never carried into a run of
  another
- a volume keeps the weight of its live volume run: a dead run, a
  forced re-read and the pages a curator changed are read with it
- ``enqueue_yolo_detect --model`` reads the named volumes and no other,
  and a second call pays nothing
- the rows of a ``bl_hein`` run are the bl-warm family for the gates
"""

import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from scanning import findings, services, yolo
from scanning.factories import ScanFactory
from scanning.models import (
    ApplyRun,
    Detection,
    ExternalJob,
    JobStatus,
    Status,
)
from scanning.tests.test_detection_relabel import finish
from scanning.tests.test_findings import make_detection, make_scan
from scanning.tests.test_jobs import make_manifest

HEIN = ["bl_hein"]

EDIT_MANIFEST = {
    "version": 1,
    "source": {"name": "page edits", "size_bytes": 10, "page_count": 1},
    "shards": [
        {
            "name": "e5.pdf",
            "index": 0,
            "key": "jobs/apply/pages/e5.pdf",
            "edit_id": 5,
            "from_page": 0,
            "to_page": 0,
            "page_count": 1,
            "size_bytes": 10,
            "source_page_count": 1,
        }
    ],
}


class DetectStubs(TestCase):
    """A bucket that holds every result, and a stage that is on."""

    def setUp(self):
        super().setUp()
        self.manifest = make_manifest(shard_count=2, pages_per_shard=10)
        for name, value in (
            ("scanning.yolo.enabled", True),
            ("scanning.s3_sync.s3_active", True),
            ("scanning.s3_sync.object_exists", True),
        ):
            patcher = patch(name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        committed = patch(
            "scanning.sharding.committed_manifest",
            return_value=(self.manifest, ""),
        )
        self.committed = committed.start()
        self.addCleanup(committed.stop)

    def read_volume(self, models=None, **scan_fields):
        """Create a scan whose detection run is read and merged.

        :param models: The weights of the run, or None for the default.
        :param scan_fields: Fields of the scan.
        :returns: The scan.
        """
        scan_fields.setdefault("status", Status.READY_FOR_REDACTION_REVIEW)
        scan = ScanFactory(page_count=20, **scan_fields)
        finish(yolo.ensure_detect_jobs(scan, self.manifest, models=models))
        return scan


class TestTheModelInTheIdentity(DetectStubs):
    """Only a run of another weight names it."""

    def test_a_default_run_names_no_model_and_sends_bl_warm(self):
        rows = yolo.ensure_detect_jobs(ScanFactory(), self.manifest)

        self.assertNotIn("models", rows[0].input_manifest)
        self.assertEqual(yolo.run_models(rows), ["bl_warm"])
        payload = yolo.build_payload(rows[0], "in", "out")
        self.assertEqual(payload["models"], ["bl_warm"])

    def test_a_hein_run_names_its_model_and_sends_it(self):
        rows = yolo.ensure_detect_jobs(
            ScanFactory(), self.manifest, models=HEIN
        )

        self.assertEqual(rows[0].input_manifest["models"], HEIN)
        self.assertEqual(yolo.run_models(rows), HEIN)
        payload = yolo.build_payload(rows[0], "in", "out")
        self.assertEqual(payload["models"], HEIN)

    def test_an_existing_bl_warm_run_is_reused_not_restarted(self):
        # Every live run of the corpus names no model. Were the default
        # written into the identity, each would read as another shard
        # set and the next caller would pay for it again.
        scan = self.read_volume()
        before = yolo.live_detect_jobs(scan)

        again = yolo.ensure_detect_jobs(scan, self.manifest)

        self.assertEqual([r.pk for r in again], [r.pk for r in before])

    def test_a_bl_warm_result_is_never_carried_into_a_hein_run(self):
        scan = self.read_volume()

        rows = yolo.ensure_detect_jobs(scan, self.manifest, models=HEIN)

        self.assertEqual(rows[0].run, 2)
        self.assertEqual({r.status for r in rows}, {JobStatus.PENDING})

    def test_a_hein_result_is_never_carried_into_a_bl_warm_run(self):
        scan = self.read_volume(models=HEIN)

        rows = yolo.ensure_detect_jobs(scan, self.manifest, models=["bl_warm"])

        self.assertEqual(rows[0].run, 2)
        self.assertEqual({r.status for r in rows}, {JobStatus.PENDING})
        self.assertNotIn("models", rows[0].input_manifest)


class TestAVolumeKeepsItsModel(DetectStubs):
    """``yolo.models_for`` is the live volume run's weight."""

    def test_a_volume_with_no_run_reads_with_the_default(self):
        self.assertEqual(yolo.models_for(ScanFactory()), ["bl_warm"])

    def test_a_caller_that_names_no_model_gets_the_hein_run_back(self):
        # The sweep, the apply and the dead-run mode name none. Read
        # with the default, the Hein run would not describe the shard
        # set and a bl-warm run would replace it.
        scan = self.read_volume(models=HEIN)
        before = yolo.live_detect_jobs(scan)

        again = yolo.ensure_detect_jobs(scan, self.manifest)

        self.assertEqual(yolo.models_for(scan), HEIN)
        self.assertEqual([r.pk for r in again], [r.pk for r in before])

    def test_a_dead_hein_run_is_read_again_with_bl_hein(self):
        scan = self.read_volume(models=HEIN)
        dead = yolo.live_detect_jobs(scan)[1]
        ExternalJob.objects.filter(pk=dead.pk).update(status=JobStatus.FAILED)

        rows = yolo.ensure_detect_jobs(scan, self.manifest)

        self.assertEqual(rows[0].run, 2)
        self.assertEqual(yolo.run_models(rows), HEIN)
        # The shard that was read is carried, the dead one is paid.
        self.assertEqual(
            [r.status for r in rows],
            [JobStatus.COMPLETED, JobStatus.PENDING],
        )

    def test_a_forced_re_read_keeps_the_model_and_carries_its_results(self):
        scan = self.read_volume(models=HEIN)

        rows = yolo.ensure_detect_jobs(scan, self.manifest, force_new_run=True)

        self.assertEqual(rows[0].run, 2)
        self.assertEqual(yolo.run_models(rows), HEIN)
        self.assertEqual({r.status for r in rows}, {JobStatus.COMPLETED})

    def test_the_edited_pages_of_a_hein_volume_are_read_with_bl_hein(self):
        scan = self.read_volume(models=HEIN)
        run = ApplyRun.objects.create(scan=scan, number=1)

        rows = yolo.ensure_detect_jobs(scan, EDIT_MANIFEST, apply_run=run)

        self.assertEqual(yolo.run_models(rows), HEIN)
        # The edited pages' rows are no volume run: the model is still
        # the volume's.
        self.assertEqual(yolo.models_for(scan), HEIN)


class TestTheModelCommand(DetectStubs):
    """``enqueue_yolo_detect --model``."""

    def _call(self, *args):
        out, err = StringIO(), StringIO()
        call_command("enqueue_yolo_detect", *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_a_named_volume_is_read_again_with_nothing_carried(self):
        scan = self.read_volume()

        out, _ = self._call(str(scan.pk), "--model", "bl_hein")

        rows = yolo.live_detect_jobs(scan)
        self.assertEqual(rows[0].run, 2)
        self.assertEqual(yolo.run_models(rows), HEIN)
        self.assertEqual({r.status for r in rows}, {JobStatus.PENDING})
        self.assertIn("2 of 2 shard(s) to read again", out)
        self.assertIn("1 run(s) started", out)

    def test_no_other_volume_is_read_again(self):
        named = self.read_volume()
        other = self.read_volume()

        self._call(str(named.pk), "--model", "bl_hein")

        rows = yolo.live_detect_jobs(other)
        self.assertEqual(rows[0].run, 1)
        self.assertEqual(yolo.run_models(rows), ["bl_warm"])

    def test_a_second_call_starts_nothing(self):
        scan = self.read_volume()
        self._call(str(scan.pk), "--model", "bl_hein")
        finish(yolo.live_detect_jobs(scan))

        out, _ = self._call(str(scan.pk), "--model", "bl_hein")

        self.assertEqual(yolo.live_detect_jobs(scan)[0].run, 2)
        self.assertIn("0 run(s) started, 0 adopted, 1 current", out)

    def test_a_run_still_working_is_left_alone(self):
        scan = self.read_volume()
        self._call(str(scan.pk), "--model", "bl_hein")

        _, err = self._call(str(scan.pk), "--model", "bl_hein")

        self.assertEqual(yolo.live_detect_jobs(scan)[0].run, 2)
        self.assertIn("is still working", err)

    def test_a_volume_with_no_run_gets_its_first_one_with_the_model(self):
        scan = ScanFactory(status=Status.AWAITING_VALIDATION, page_count=20)

        self._call(str(scan.pk), "--model", "bl_hein")

        rows = yolo.live_detect_jobs(scan)
        self.assertEqual(rows[0].run, 1)
        self.assertEqual(yolo.run_models(rows), HEIN)

    def test_an_approved_volume_is_read_again(self):
        scan = self.read_volume(status=Status.REDACTION_REVIEW_DONE)

        self._call(str(scan.pk), "--model", "bl_hein")

        self.assertEqual(yolo.run_models(yolo.live_detect_jobs(scan)), HEIN)

    def test_the_edited_pages_are_read_again_with_the_model_too(self):
        # Their boxes go into the corrected volume's document beside
        # the volume's, and one document of two weights is what the
        # gates cannot judge.
        scan = self.read_volume()
        run = ApplyRun.objects.create(
            scan=scan,
            number=1,
            built_at=timezone.now(),
            detections_key="a1/detections-volume.json",
        )
        finish(yolo.ensure_detect_jobs(scan, EDIT_MANIFEST, apply_run=run))

        with patch(
            "scanning.apply.stored_shard_manifest", return_value=EDIT_MANIFEST
        ):
            out, _ = self._call(str(scan.pk), "--model", "bl_hein")

        edit_rows = ExternalJob.objects.filter(apply_run=run).order_by("-run")
        self.assertEqual(yolo.run_models([edit_rows[0]]), HEIN)
        self.assertEqual(edit_rows[0].status, JobStatus.PENDING)
        self.assertIn("volume, edited pages", out)

    def test_a_dry_run_changes_nothing(self):
        scan = self.read_volume()

        out, _ = self._call(str(scan.pk), "--model", "bl_hein", "--dry-run")

        self.assertIn(f"scan {scan.pk}: would read again (volume)", out)
        self.assertEqual(yolo.live_detect_jobs(scan)[0].run, 1)
        self.committed.assert_not_called()

    def test_bl_warm_takes_a_hein_volume_back_and_carries_the_old_read(self):
        scan = self.read_volume()
        self._call(str(scan.pk), "--model", "bl_hein")
        finish(yolo.live_detect_jobs(scan))

        self._call(str(scan.pk), "--model", "bl_warm")

        rows = yolo.live_detect_jobs(scan)
        self.assertEqual(rows[0].run, 3)
        self.assertEqual(yolo.run_models(rows), ["bl_warm"])
        self.assertEqual({r.status for r in rows}, {JobStatus.COMPLETED})

    def test_the_scans_come_from_a_file_too(self):
        first, second = self.read_volume(), self.read_volume()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "hein.txt"
            path.write_text(f"# the Hein volumes\n{first.pk}\n\n{second.pk}\n")
            self._call("--scans-file", str(path), "--model", "bl_hein")

        for scan in (first, second):
            self.assertEqual(
                yolo.run_models(yolo.live_detect_jobs(scan)), HEIN
            )

    def test_a_file_line_that_is_no_number_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "hein.txt"
            path.write_text("12\nvolume 13\n")
            with self.assertRaisesMessage(CommandError, "line 2"):
                self._call("--scans-file", str(path), "--model", "bl_hein")

    def test_a_weight_the_image_does_not_bake_is_refused(self):
        scan = self.read_volume()

        with self.assertRaises(CommandError):
            self._call(str(scan.pk), "--model", "large")

        self.assertEqual(yolo.live_detect_jobs(scan)[0].run, 1)

    def test_the_model_needs_named_scans(self):
        with self.assertRaisesMessage(CommandError, "named volumes"):
            self._call("--model", "bl_hein")

    def test_the_model_goes_with_no_other_mode(self):
        for mode in ("--stale-labels", "--dead-runs"):
            with self.assertRaisesMessage(CommandError, "--model goes with"):
                self._call("1", "--model", "bl_hein", mode)


class TestTheFamilyOfAHeinRun(TestCase):
    """A ``bl_hein`` row takes the bl-warm gates."""

    def test_hein_rows_are_the_bl_warm_family(self):
        scan = make_scan()
        make_detection(
            scan,
            "KEY_ICON",
            1,
            model_name=Detection.ModelName.BL_HEIN,
            found_by=[{"model": "bl_hein", "confidence": 0.9}],
        )

        self.assertTrue(findings.model_rows_are_bl_warm(scan))

    def test_a_row_of_another_family_keeps_the_legacy_gates(self):
        scan = make_scan()
        make_detection(
            scan,
            "KEY_ICON",
            1,
            found_by=[{"model": "bl_hein", "confidence": 0.9}],
        )
        make_detection(
            scan,
            "HEADNOTE",
            2,
            found_by=[{"model": "small", "confidence": 0.4}],
        )

        self.assertFalse(findings.model_rows_are_bl_warm(scan))

    def test_the_import_stamps_the_weight_that_drew_the_run(self):
        def entries(*models):
            return [
                {"found_by": [{"model": model, "confidence": 0.9}]}
                for model in models
            ]

        self.assertEqual(
            services._run_model_name(entries("bl_hein", "bl_hein")),
            Detection.ModelName.BL_HEIN,
        )
        self.assertEqual(
            services._run_model_name(entries("bl_warm")),
            Detection.ModelName.BL_WARM,
        )
        # Two weights of the family in one document: the family's name.
        self.assertEqual(
            services._run_model_name(entries("bl_warm", "bl_hein")),
            Detection.ModelName.BL_WARM,
        )
        self.assertEqual(services._run_model_name(entries("small")), "")
        self.assertEqual(services._run_model_name([]), "")
