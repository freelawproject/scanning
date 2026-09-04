"""Tests for ``enqueue_yolo_detect`` (#250): the operator's way to run
detection again over a volume whose run died.

The sweep starts one run per shard set and never a second. This
command is the deliberate second; what is pinned here is that it takes
only what it is asked for, pays only for the shards that died, and
changes nothing under ``--dry-run``.
"""

from io import StringIO
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from scanning import yolo
from scanning.factories import ScanFactory
from scanning.models import ExternalJob, JobStatus, Status
from scanning.tests.test_jobs import make_manifest


class TestEnqueueYoloDetect(TestCase):
    """One fresh run per dead run, and nothing else."""

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

    def _scan_with_run(self, dead=True, status=Status.AWAITING_VALIDATION):
        scan = ScanFactory(status=status, page_count=20)
        rows = yolo.ensure_detect_jobs(scan, self.manifest)
        ExternalJob.objects.filter(pk=rows[0].pk).update(
            status=JobStatus.COMPLETED,
            result_key="jobs/detect/blackletter/r1-s0-a1.json",
            completed_at=timezone.now(),
        )
        ExternalJob.objects.filter(pk=rows[1].pk).update(
            status=JobStatus.FAILED if dead else JobStatus.COMPLETED,
            result_key="jobs/detect/blackletter/r1-s1-a1.json",
            completed_at=timezone.now(),
        )
        return scan

    def _call(self, *args):
        out, err = StringIO(), StringIO()
        call_command("enqueue_yolo_detect", *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_a_dead_run_is_replaced_and_the_good_shard_is_carried(self):
        scan = self._scan_with_run()
        out, _ = self._call("--dead-runs")
        rows = yolo.live_detect_jobs(scan)
        self.assertEqual(rows[0].run, 2)
        self.assertEqual(rows[0].status, JobStatus.COMPLETED)
        self.assertEqual(rows[1].status, JobStatus.PENDING)
        self.assertIn("1 of 2 shard(s) to detect again", out)
        self.assertIn("1 run(s) started", out)

    def test_a_dry_run_reports_and_creates_nothing(self):
        scan = self._scan_with_run()
        out, _ = self._call("--dead-runs", "--dry-run")
        self.assertIn(f"scan {scan.pk}: would start a fresh run", out)
        self.assertEqual(yolo.live_detect_jobs(scan)[0].run, 1)
        self.committed.assert_not_called()

    def test_a_live_run_is_not_touched_by_dead_runs(self):
        scan = self._scan_with_run(dead=False)
        out, _ = self._call("--dead-runs")
        self.assertEqual(yolo.live_detect_jobs(scan)[0].run, 1)
        self.assertIn("0 run(s) started, 1 skipped", out)

    def test_a_named_scan_with_a_live_run_hears_why(self):
        scan = self._scan_with_run(dead=False)
        _, err = self._call(str(scan.pk))
        self.assertIn(f"scan {scan.pk}: run 1 is not dead", err)
        self.assertEqual(yolo.live_detect_jobs(scan)[0].run, 1)

    def test_a_named_scan_with_no_run_gets_one(self):
        scan = ScanFactory(status=Status.ERROR, page_count=20)
        self._call(str(scan.pk))
        self.assertEqual(len(yolo.live_detect_jobs(scan)), 2)

    def test_the_limit_bounds_the_runs_started(self):
        scans = [self._scan_with_run() for _ in range(3)]
        out, _ = self._call("--dead-runs", "--limit", "2")
        self.assertIn("2 run(s) started", out)
        self.assertEqual(
            sum(1 for s in scans if yolo.live_detect_jobs(s)[0].run == 2), 2
        )

    def test_an_approved_volume_is_out_of_dead_runs(self):
        scan = self._scan_with_run(status=Status.APPROVED)
        self._call("--dead-runs")
        self.assertEqual(yolo.live_detect_jobs(scan)[0].run, 1)

    def test_a_stale_shard_set_is_refused_not_re_cut(self):
        scan = self._scan_with_run()
        self.committed.return_value = (None, "The original PDF has changed")
        _, err = self._call("--dead-runs")
        self.assertIn("The original PDF has changed", err)
        self.assertEqual(yolo.live_detect_jobs(scan)[0].run, 1)

    def test_an_unknown_scan_is_named(self):
        _, err = self._call("999999")
        self.assertIn("scan 999999: no such scan", err)

    def test_nothing_selected_is_an_error(self):
        with self.assertRaises(CommandError):
            self._call()

    def test_a_disabled_stage_refuses_to_queue_rows(self):
        self._scan_with_run()
        with patch("scanning.yolo.enabled", return_value=False):
            with self.assertRaises(CommandError):
                self._call("--dead-runs")

    def test_the_command_makes_no_call_to_runpod(self):
        self._scan_with_run()
        with patch("scanning.runpod_client.submit_job") as submit:
            self._call("--dead-runs")
        submit.assert_not_called()
