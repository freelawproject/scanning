"""Tests for the ``reread_failed_pages`` command (issue #238).

The command is the backfill for runs whose worker left pages unread. It
starts a forced new dots.mocr run, and the carry rule re-pays only the
shards with a hole. Under test: which scans it takes, what a dry run
touches, and the refusals.
"""

from io import StringIO
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import TestCase

from scanning import dots_mocr
from scanning.factories import ScanFactory
from scanning.models import ExternalJob, JobStatus, Status
from scanning.tests.test_jobs import make_manifest


class TestRereadFailedPages(TestCase):
    """One forced run per volume with a hole, and nothing else."""

    def setUp(self):
        super().setUp()
        self.manifest = make_manifest(shard_count=2, pages_per_shard=10)
        for name, value in (
            ("scanning.dots_mocr.enabled", True),
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

    def _glued_scan(
        self, holes=(1,), filtered=(), status=Status.AWAITING_VALIDATION
    ):
        """Create a scan whose live run is consumed, with ``holes``."""
        scan = ScanFactory(status=status, page_count=20)
        for row in dots_mocr.ensure_analyze_jobs(scan, self.manifest):
            output = {"page_count": 10, "failed_pages": []}
            if row.shard_index in holes:
                output["failed_pages"] = [4]
            if row.shard_index in filtered:
                output["filtered_pages"] = [8]
            ExternalJob.objects.filter(pk=row.pk).update(
                status=JobStatus.CONSUMED,
                result_key=f"r1-s{row.shard_index}-a1.json",
                provider_meta={"output": output},
            )
        return scan

    def _call(self, *args):
        out, err = StringIO(), StringIO()
        call_command("reread_failed_pages", *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_a_forced_run_re_reads_the_shards_with_a_hole_only(self):
        scan = self._glued_scan(holes=(1,))

        out, _ = self._call()

        rows = dots_mocr.live_analyze_jobs(scan)
        self.assertEqual(rows[0].run, 2)
        self.assertEqual(rows[0].status, JobStatus.COMPLETED)
        self.assertEqual(rows[1].status, JobStatus.PENDING)
        self.assertIn("1 of 2 shard(s) to read again", out)
        self.assertIn("shard(s) 2 had unread pages", out)
        self.assertIn("1 scan(s) to read again", out)

    def test_a_filtered_page_is_read_again_too(self):
        # No cell, no page number: the same hole as a failed page.
        scan = self._glued_scan(holes=(), filtered=(0,))

        out, _ = self._call()

        rows = dots_mocr.live_analyze_jobs(scan)
        self.assertEqual(rows[0].run, 2)
        self.assertEqual(rows[0].status, JobStatus.PENDING)
        self.assertEqual(rows[1].status, JobStatus.COMPLETED)
        self.assertIn("shard(s) 1 had unread pages", out)

    def test_a_second_run_does_not_pay_for_the_same_answer(self):
        # Run 2 re-read shard 2 and got the same hole. The worker is
        # deterministic, so run 3 would buy the same answer.
        scan = self._glued_scan(holes=(1,))
        self._call()
        for row in dots_mocr.live_analyze_jobs(scan):
            if row.status == JobStatus.PENDING:
                ExternalJob.objects.filter(pk=row.pk).update(
                    result_key="r2-s1-a1.json",
                    provider_meta={
                        "output": {"page_count": 10, "failed_pages": [4]}
                    },
                )
        ExternalJob.objects.filter(scan=scan, run=2).update(
            status=JobStatus.CONSUMED
        )

        out, err = self._call(str(scan.pk))

        self.assertEqual(dots_mocr.live_analyze_jobs(scan)[0].run, 2)
        self.assertIn("0 scan(s) to read again, 1 skipped", out)
        self.assertIn("run 2 already read the unread pages again", err)

    def test_a_dry_run_reports_and_creates_nothing(self):
        scan = self._glued_scan(holes=(0, 1))

        out, _ = self._call("--dry-run")

        self.assertIn(f"scan {scan.pk}: would read shard(s) 1, 2 of 2", out)
        self.assertEqual(dots_mocr.live_analyze_jobs(scan)[0].run, 1)
        self.assertEqual(ExternalJob.objects.count(), 2)
        self.committed.assert_not_called()

    def test_a_run_without_holes_is_skipped(self):
        scan = self._glued_scan(holes=())

        out, err = self._call()

        self.assertEqual(dots_mocr.live_analyze_jobs(scan)[0].run, 1)
        self.assertIn("0 scan(s) to read again, 1 skipped", out)
        # Unnamed, so a count is enough.
        self.assertEqual(err, "")

    def test_a_named_scan_hears_why_it_was_skipped(self):
        scan = self._glued_scan(holes=())

        out, err = self._call(str(scan.pk))

        self.assertIn("1 skipped", out)
        self.assertIn(
            f"scan {scan.pk}: no shard of the live run reports unread pages",
            err,
        )

    def test_an_approved_volume_is_left_alone(self):
        scan = self._glued_scan(status=Status.PAGE_COMPLETENESS_REVIEW_DONE)

        out, _ = self._call()

        self.assertEqual(dots_mocr.live_analyze_jobs(scan)[0].run, 1)
        self.assertIn("0 scan(s) to read again", out)

    def test_a_ready_volume_is_read_again(self):
        # READY is a recompute for the apply: data only, status kept.
        scan = self._glued_scan(
            status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )

        self._call()

        self.assertEqual(dots_mocr.live_analyze_jobs(scan)[0].run, 2)

    def test_named_scans_only(self):
        wanted = self._glued_scan()
        other = self._glued_scan()

        out, err = self._call(str(wanted.pk), "99999")

        self.assertEqual(dots_mocr.live_analyze_jobs(wanted)[0].run, 2)
        self.assertEqual(dots_mocr.live_analyze_jobs(other)[0].run, 1)
        self.assertIn("scan 99999: no glued run in review 1", err)

    def test_a_stale_shard_set_is_refused_not_re_cut(self):
        scan = self._glued_scan()
        self.committed.return_value = (None, "the original moved")

        out, err = self._call()

        self.assertEqual(dots_mocr.live_analyze_jobs(scan)[0].run, 1)
        self.assertIn(f"scan {scan.pk}: the original moved", err)
        self.assertIn("1 skipped", out)

    def test_a_disabled_stage_refuses_to_queue_rows(self):
        self._glued_scan()
        with patch("scanning.dots_mocr.enabled", return_value=False):
            with self.assertRaises(CommandError):
                self._call()
        # A dry run still answers.
        with patch("scanning.dots_mocr.enabled", return_value=False):
            out, _ = self._call("--dry-run")
        self.assertIn("would read", out)
