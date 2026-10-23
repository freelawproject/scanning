"""Tests for the re-read of the detection classes (#338).

A detection run read before ``yolo.LABEL_SET`` lacks two of bl-warm's
classes, and nothing but a new read recovers them. Pinned here:

- the class set is part of a row's identity: a result read without it
  is never carried, and a run read without it is still reused, so no
  tick re-pays it
- a second volume run writes the corrected volume's detections again
  before it is consumed, or the compute reads the first run's document
- ``enqueue_yolo_detect --stale-labels`` takes only what it is asked
  for, and ``--read-since`` adopts a run instead of paying for it
- ``reopen_redaction_review`` takes an approved volume back to review 2
"""

from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from scanning import apply, jobs, yolo
from scanning.factories import OpinionFactory, ScanFactory
from scanning.models import (
    ApplyRun,
    DetectionDecision,
    ExternalJob,
    JobEngine,
    JobStage,
    JobStatus,
    OpinionReviewStatus,
    Scan,
    Status,
)
from scanning.tests.test_jobs import make_manifest
from scanning.tests.test_yolo_merge import (
    DetectJobsMixin,
    make_detection,
    make_envelope,
)


def identity_map(page_count: int) -> dict:
    """Build the page map of a run with no structural edit.

    :param page_count: Pages of the original.
    :returns: A stored page map every page of which is in its place.
    :rtype: dict
    """
    return {
        "source_page_count": page_count,
        "final_page_count": page_count,
        "pages": [
            {
                "final_page": page,
                "source": {"kind": "original", "pdf_page": page},
            }
            for page in range(1, page_count + 1)
        ],
    }


def finish(rows, status=JobStatus.CONSUMED, submitted_at=None) -> None:
    """Mark a run's rows as read, with a result key each.

    :param rows: One run's rows.
    :param status: The status to leave them in.
    :param submitted_at: When they were handed to the provider.
    :return: None.
    """
    for row in rows:
        ExternalJob.objects.filter(pk=row.pk).update(
            status=status,
            result_key=f"jobs/detect/blackletter/r{row.run}-s{row.shard_index}-a1.json",
            submitted_at=submitted_at or timezone.now(),
            completed_at=timezone.now(),
        )


def strip_label_set(rows) -> None:
    """Make a run look read before ``label_set`` joined the identity.

    :param rows: One run's rows.
    :return: None.
    """
    for row in rows:
        manifest = dict(row.input_manifest)
        manifest.pop("label_set")
        ExternalJob.objects.filter(pk=row.pk).update(input_manifest=manifest)


class TestLabelSetIdentity(TestCase):
    """The class set is identity: strict for the carry, lenient for reuse."""

    def setUp(self):
        super().setUp()
        self.manifest = make_manifest(shard_count=2, pages_per_shard=5)
        for name in (
            "scanning.s3_sync.s3_active",
            "scanning.s3_sync.object_exists",
        ):
            patcher = patch(name, return_value=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_every_row_carries_the_class_set(self):
        rows = yolo.ensure_detect_jobs(ScanFactory(), self.manifest)
        self.assertTrue(yolo.labels_current(rows))
        self.assertEqual(rows[0].input_manifest["label_set"], yolo.LABEL_SET)

    def test_a_run_read_before_the_stamp_is_still_reused(self):
        # Every live run of the corpus lacks the field on deploy day. A
        # strict match would read each one as stale, and the next caller
        # would re-pay it.
        scan = ScanFactory()
        rows = yolo.ensure_detect_jobs(scan, self.manifest)
        finish(rows)
        strip_label_set(rows)

        again = yolo.ensure_detect_jobs(scan, self.manifest)

        self.assertEqual([r.pk for r in again], [r.pk for r in rows])
        self.assertFalse(yolo.labels_current(again))

    def test_a_result_read_before_the_stamp_is_never_carried(self):
        scan = ScanFactory()
        rows = yolo.ensure_detect_jobs(scan, self.manifest)
        finish(rows)
        strip_label_set(rows)

        fresh = yolo.ensure_detect_jobs(
            scan, self.manifest, force_new_run=True
        )

        self.assertEqual(fresh[0].run, 2)
        self.assertEqual({r.status for r in fresh}, {JobStatus.PENDING})
        self.assertTrue(yolo.labels_current(fresh))

    def test_a_result_read_with_the_stamp_is_carried(self):
        scan = ScanFactory()
        rows = yolo.ensure_detect_jobs(scan, self.manifest)
        finish(rows)

        fresh = yolo.ensure_detect_jobs(
            scan, self.manifest, force_new_run=True
        )

        self.assertEqual({r.status for r in fresh}, {JobStatus.COMPLETED})

    def test_the_other_stages_carry_no_class_set(self):
        rows = jobs.ensure_convert_jobs(ScanFactory(), self.manifest)
        self.assertNotIn("label_set", rows[0].input_manifest)


class TestRowsByEdit(TestCase):
    """The newest read of an edited page wins its edit."""

    def test_the_newest_run_wins(self):
        old = ExternalJob(run=1, attempt=1, stage=JobStage.DETECT)
        old.input_manifest = {"edit_id": 7}
        new = ExternalJob(run=3, attempt=1, stage=JobStage.DETECT)
        new.input_manifest = {"edit_id": 7}
        for order in ([old, new], [new, old]):
            with self.subTest(order=[r.run for r in order]):
                self.assertIs(
                    apply._rows_by_edit(order, JobStage.DETECT)[7], new
                )


class TestRefreshOnMerge(DetectJobsMixin, TestCase):
    """A second volume run writes the corrected volume's detections."""

    def build_with_run(self):
        """Build a scan whose first run is merged into an identity run.

        :returns: ``(scan, apply run, second run's rows)``.
        """
        scan, rows = self.build(shard_count=2, pages_per_shard=2)
        self.assertEqual(yolo.finish_ready_runs(), 1)
        run = ApplyRun.objects.create(
            scan=scan,
            number=1,
            built_at=timezone.now(),
            page_map=identity_map(4),
            bitonal_key="bitonal.pdf",
            ocr_key="ocr.json",
            printed_pages_key="printed.json",
            detections_key=yolo.merged_result_key(scan, rows[0].run),
        )
        with patch("scanning.s3_sync.object_exists", return_value=False):
            second = yolo.ensure_detect_jobs(
                scan, make_manifest(2, 2), force_new_run=True
            )
        for index, job in enumerate(second):
            job.status = JobStatus.COMPLETED
            job.result_key = f"jobs/detect/blackletter/r2-s{index}-a1.json"
            job.save()
            self.write_envelope(
                index,
                make_envelope(
                    job,
                    [make_detection(0), make_detection(1, "HEADING")],
                    2,
                ),
            )
        return scan, run, yolo.live_detect_jobs(scan)

    def test_the_standing_run_reads_the_new_merge(self):
        scan, run, second = self.build_with_run()

        self.assertEqual(yolo.finish_ready_runs(), 1)

        run.refresh_from_db()
        self.assertEqual(
            run.detections_key, yolo.merged_result_key(scan, second[0].run)
        )
        self.assertEqual(
            {r.status for r in yolo.live_detect_jobs(scan)},
            {JobStatus.CONSUMED},
        )

    def test_a_first_merge_leaves_an_unglued_run_to_the_glue(self):
        scan, _ = self.build(shard_count=1, pages_per_shard=2)
        run = ApplyRun.objects.create(
            scan=scan,
            number=1,
            built_at=timezone.now(),
            page_map=identity_map(2),
        )

        yolo.finish_ready_runs()

        run.refresh_from_db()
        self.assertEqual(run.detections_key, "")

    def test_a_run_of_another_original_is_left_alone(self):
        scan, run, _ = self.build_with_run()
        before = run.detections_key
        Scan.objects.filter(pk=scan.pk).update(source_fingerprint="new")
        ApplyRun.objects.filter(pk=run.pk).update(source_fingerprint="old")

        self.assertEqual(yolo.finish_ready_runs(), 1)

        run.refresh_from_db()
        self.assertEqual(run.detections_key, before)

    def test_the_merge_waits_for_a_re_read_of_the_edited_pages(self):
        scan, run, second = self.build_with_run()
        ExternalJob.objects.create(
            scan=scan,
            stage=JobStage.DETECT,
            engine=JobEngine.BLACKLETTER,
            run=9,
            shard_index=0,
            shard_count=1,
            apply_run=run,
            input_key="jobs/apply/pages/e1.pdf",
            input_manifest={"edit_id": 1},
            status=JobStatus.SUBMITTED,
        )
        self.download.reset_mock()

        self.assertEqual(yolo.finish_ready_runs(), 0)

        self.download.assert_not_called()
        self.assertEqual(
            {r.status for r in yolo.live_detect_jobs(scan)},
            {JobStatus.COMPLETED},
        )
        self.assertEqual(yolo._merge_attempts(second), 0)

    def _edit_row(self, run, job_run, status):
        return ExternalJob.objects.create(
            scan=run.scan,
            stage=JobStage.DETECT,
            engine=JobEngine.BLACKLETTER,
            run=job_run,
            shard_index=0,
            shard_count=1,
            apply_run=run,
            input_key="jobs/apply/pages/e1.pdf",
            input_manifest={"edit_id": 1},
            status=status,
        )

    def test_a_dead_re_read_of_the_edited_pages_holds_the_merge(self):
        # It has no result to read, so a glue past it fails the same way
        # on every tick and spends the merge ledger.
        scan, run, second = self.build_with_run()
        self._edit_row(run, 9, JobStatus.FAILED)
        self.download.reset_mock()

        self.assertEqual(yolo.finish_ready_runs(), 0)

        self.download.assert_not_called()
        self.assertEqual(yolo._merge_attempts(second), 0)

    def test_a_dead_earlier_read_is_not_the_live_one(self):
        scan, run, _ = self.build_with_run()
        self._edit_row(run, 8, JobStatus.FAILED)
        self._edit_row(run, 9, JobStatus.CONSUMED)

        self.assertFalse(apply.detections_refresh_waits(scan))

    def test_a_failed_refresh_counts_on_the_merge_and_consumes_nothing(self):
        scan, _, second = self.build_with_run()

        with patch(
            "scanning.apply._glue_detections",
            side_effect=apply.ApplyError("boom"),
        ):
            self.assertEqual(yolo.finish_ready_runs(), 0)

        rows = yolo.live_detect_jobs(scan)
        self.assertEqual({r.status for r in rows}, {JobStatus.COMPLETED})
        self.assertEqual(yolo._merge_attempts(rows), 1)


class TestStaleLabels(TestCase):
    """``enqueue_yolo_detect --stale-labels``."""

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

    def _scan(
        self,
        status=Status.READY_FOR_REDACTION_REVIEW,
        stale=True,
        submitted_at=None,
    ):
        scan = ScanFactory(status=status, page_count=20)
        rows = yolo.ensure_detect_jobs(scan, self.manifest)
        finish(rows, submitted_at=submitted_at)
        if stale:
            strip_label_set(rows)
        return scan

    def _call(self, *args):
        out, err = StringIO(), StringIO()
        call_command(
            "enqueue_yolo_detect",
            "--stale-labels",
            *args,
            stdout=out,
            stderr=err,
        )
        return out.getvalue(), err.getvalue()

    def test_a_stale_run_is_read_again_with_nothing_carried(self):
        scan = self._scan()
        out, _ = self._call()
        rows = yolo.live_detect_jobs(scan)
        self.assertEqual(rows[0].run, 2)
        self.assertEqual({r.status for r in rows}, {JobStatus.PENDING})
        self.assertIn("2 of 2 shard(s) to read again", out)

    def test_an_approved_volume_is_taken(self):
        scan = self._scan(status=Status.REDACTION_REVIEW_DONE)
        self._call()
        self.assertEqual(yolo.live_detect_jobs(scan)[0].run, 2)

    def test_a_volume_owned_by_the_daemon_is_not(self):
        scan = self._scan(status=Status.PROCESSING)
        self._call()
        self.assertEqual(yolo.live_detect_jobs(scan)[0].run, 1)

    def test_a_current_run_is_left_alone(self):
        scan = self._scan(stale=False)
        out, _ = self._call()
        self.assertEqual(yolo.live_detect_jobs(scan)[0].run, 1)
        self.assertIn("0 run(s) started, 0 adopted, 1 current", out)

    def test_an_open_run_is_not_replaced(self):
        # Its rows would still be submitted, and paid beside the new run.
        scan = self._scan()
        rows = yolo.live_detect_jobs(scan)
        ExternalJob.objects.filter(pk=rows[0].pk).update(
            status=JobStatus.SUBMITTED
        )
        _, err = self._call()
        self.assertIn("still working", err)
        self.assertEqual(yolo.live_detect_jobs(scan)[0].run, 1)

    def test_an_excluded_scan_is_left_out(self):
        scan = self._scan()
        other = self._scan()
        self._call("--exclude", str(scan.pk))
        self.assertEqual(yolo.live_detect_jobs(scan)[0].run, 1)
        self.assertEqual(yolo.live_detect_jobs(other)[0].run, 2)

    def test_named_scans_narrow_the_selection(self):
        scan = self._scan()
        other = self._scan()
        self._call(str(other.pk))
        self.assertEqual(yolo.live_detect_jobs(scan)[0].run, 1)
        self.assertEqual(yolo.live_detect_jobs(other)[0].run, 2)

    def test_a_run_read_after_the_image_is_adopted_not_paid(self):
        since = timezone.now() - timedelta(days=1)
        late = self._scan(submitted_at=timezone.now())
        early = self._scan(submitted_at=since - timedelta(days=1))

        out, _ = self._call("--read-since", since.isoformat())

        rows = yolo.live_detect_jobs(late)
        self.assertEqual(rows[0].run, 1)
        self.assertTrue(yolo.labels_current(rows))
        self.assertEqual(yolo.live_detect_jobs(early)[0].run, 2)
        self.assertIn("1 run(s) started, 1 adopted", out)

    def test_a_dry_run_changes_nothing(self):
        since = timezone.now() - timedelta(days=1)
        late = self._scan(submitted_at=timezone.now())
        early = self._scan(submitted_at=since - timedelta(days=1))

        out, _ = self._call("--read-since", since.isoformat(), "--dry-run")

        self.assertIn(f"scan {early.pk}: would read again (volume)", out)
        self.assertIn(f"scan {late.pk}: would adopt", out)
        self.assertFalse(yolo.labels_current(yolo.live_detect_jobs(late)))
        self.assertEqual(yolo.live_detect_jobs(early)[0].run, 1)
        self.committed.assert_not_called()

    def test_stale_edited_pages_are_read_again_and_the_volume_carried(self):
        scan = self._scan(stale=False)
        run = ApplyRun.objects.create(
            scan=scan,
            number=1,
            built_at=timezone.now(),
            detections_key="a1/detections-volume.json",
        )
        edit_manifest = {
            "version": 1,
            "source": {
                "name": "page edits",
                "size_bytes": 10,
                "page_count": 1,
            },
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
        edit_rows = yolo.ensure_detect_jobs(scan, edit_manifest, apply_run=run)
        finish(edit_rows)
        strip_label_set(edit_rows)

        with patch(
            "scanning.apply.stored_shard_manifest", return_value=edit_manifest
        ):
            out, _ = self._call()

        new_edits = jobs.live_run(
            scan, JobStage.DETECT, JobEngine.BLACKLETTER, apply_run=run
        )
        self.assertEqual({r.status for r in new_edits}, {JobStatus.PENDING})
        volume = yolo.live_detect_jobs(scan)
        self.assertEqual(volume[0].run, max(r.run for r in new_edits) + 1)
        self.assertEqual({r.status for r in volume}, {JobStatus.COMPLETED})
        self.assertIn("(edited pages)", out)

    def test_a_dead_run_is_read_again_not_counted_current(self):
        scan = self._scan(stale=False)
        rows = yolo.live_detect_jobs(scan)
        ExternalJob.objects.filter(pk=rows[0].pk).update(
            status=JobStatus.FAILED
        )
        ExternalJob.objects.filter(pk=rows[1].pk).update(
            status=JobStatus.COMPLETED
        )

        out, _ = self._call("--read-since", timezone.now().isoformat())

        fresh = yolo.live_detect_jobs(scan)
        self.assertEqual(fresh[0].run, 2)
        # The dead shard is read again and the good one carried.
        self.assertEqual(
            [r.status for r in fresh],
            [JobStatus.PENDING, JobStatus.COMPLETED],
        )
        self.assertIn("(volume, dead)", out)
        self.assertIn("1 run(s) started, 0 adopted, 0 current", out)

    def test_it_goes_alone(self):
        with self.assertRaises(CommandError):
            self._call("--dead-runs")
        with self.assertRaises(CommandError):
            call_command("enqueue_yolo_detect", "--read-since", "2026-09-16")


class TestReopenRedactionReview(TestCase):
    """``reopen_redaction_review``."""

    def _call(self, *args):
        out, err = StringIO(), StringIO()
        call_command("reopen_redaction_review", *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_an_approved_volume_goes_back_to_review_two(self):
        scan = ScanFactory(status=Status.REDACTION_REVIEW_DONE)
        out, _ = self._call(str(scan.pk))
        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.READY_FOR_REDACTION_REVIEW)
        self.assertIn("1 reopened, 0 refused", out)

    def test_another_status_is_refused(self):
        scan = ScanFactory(status=Status.PAGE_COMPLETENESS_REVIEW_DONE)
        _, err = self._call(str(scan.pk))
        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.PAGE_COMPLETENESS_REVIEW_DONE)
        self.assertIn("not an approved redaction review", err)

    def test_an_approved_opinion_text_refuses_the_volume(self):
        scan = ScanFactory(status=Status.REDACTION_REVIEW_DONE)
        OpinionFactory(scan=scan, status=OpinionReviewStatus.TEXT_REVIEW_DONE)
        _, err = self._call(str(scan.pk))
        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.REDACTION_REVIEW_DONE)
        self.assertIn("1 opinion text(s) approved", err)

    def test_a_dry_run_changes_nothing(self):
        scan = ScanFactory(status=Status.REDACTION_REVIEW_DONE)
        out, _ = self._call(str(scan.pk), "--dry-run")
        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.REDACTION_REVIEW_DONE)
        self.assertIn("would reopen", out)

    def test_an_unknown_scan_is_named(self):
        _, err = self._call("999999")
        self.assertIn("scan 999999: no such scan", err)


class TestCompareDetectionRuns(TestCase):
    """``compare_detection_runs``: two merged runs, read and never written."""

    def setUp(self):
        super().setUp()
        self.scan = ScanFactory(page_count=2)
        manifest = make_manifest(shard_count=1, pages_per_shard=2)
        first = yolo.ensure_detect_jobs(self.scan, manifest)
        finish(first)
        with patch("scanning.s3_sync.s3_active", return_value=False):
            second = yolo.ensure_detect_jobs(
                self.scan, manifest, force_new_run=True
            )
        finish(second)
        box = [100.0, 100.0, 300.0, 200.0]
        self.documents = {
            1: [
                self._entry(1, "PAGE_HEADER", 2, box),
                self._entry(2, "KEY_ICON", 0, box),
            ],
            2: [
                self._entry(1, "PAGE_HEADER", 2, [102.0, 100.0, 302.0, 200.0]),
                self._entry(1, "HEADING", 21, [50.0, 400.0, 500.0, 450.0]),
            ],
        }
        load = patch(
            "scanning.yolo.load_merged_document",
            side_effect=lambda scan, run: {"detections": self.documents[run]},
        )
        self.load = load.start()
        self.addCleanup(load.stop)

    @staticmethod
    def _entry(page, label, label_id, bbox):
        return {
            "pdf_page": page,
            "page_index": page - 1,
            "label": label,
            "label_id": label_id,
            "bbox": bbox,
        }

    def _decision(self, page, label, label_id, bbox, **values):
        return DetectionDecision.objects.create(
            scan=self.scan,
            kind=DetectionDecision.Kind.DEACTIVATE,
            source_page=page,
            label=label,
            label_id=label_id,
            target_x0=bbox[0],
            target_y0=bbox[1],
            target_x1=bbox[2],
            target_y1=bbox[3],
            **values,
        )

    def _call(self, *args):
        out = StringIO()
        call_command(
            "compare_detection_runs", str(self.scan.pk), *args, stdout=out
        )
        return out.getvalue()

    def _line(self, out, label):
        return next(
            line.split() for line in out.splitlines() if line.startswith(label)
        )

    def test_the_live_run_is_compared_with_the_one_before(self):
        out = self._call()
        self.assertIn(f"scan {self.scan.pk}: run 1 against run 2", out)
        # label, old, new, matched, mean IoU, lost, added
        self.assertEqual(
            self._line(out, "PAGE_HEADER"),
            ["PAGE_HEADER", "1", "1", "1", "0.980", "0", "0"],
        )
        self.assertEqual(
            self._line(out, "KEY_ICON"),
            ["KEY_ICON", "1", "0", "0", "-", "1", "0"],
        )
        self.assertEqual(
            self._line(out, "HEADING"),
            ["HEADING", "0", "1", "0", "-", "0", "1"],
        )

    def test_the_standing_decisions_are_checked_against_the_new_run(self):
        self._decision(1, "PAGE_HEADER", 2, [100.0, 100.0, 300.0, 200.0])
        lost = self._decision(2, "KEY_ICON", 0, [100.0, 100.0, 300.0, 200.0])
        out = self._call()
        self.assertIn(
            "standing decisions: 1 would land, 1 would be stale, 0 on "
            "edited pages not checked",
            out,
        )
        self.assertIn(f"#{lost.pk} deactivate KEY_ICON p.2", out)

    def test_a_withdrawn_decision_is_not_counted(self):
        self._decision(
            2,
            "KEY_ICON",
            0,
            [100.0, 100.0, 300.0, 200.0],
            withdrawn_at=timezone.now(),
        )
        out = self._call()
        self.assertIn("0 would land, 0 would be stale", out)

    def test_a_run_that_is_not_merged_is_refused(self):
        with self.assertRaises(CommandError):
            self._call("--new", "5")

    def test_it_writes_nothing(self):
        before = list(
            ExternalJob.objects.order_by("pk").values_list(
                "status", "input_manifest"
            )
        )
        self._call()
        after = list(
            ExternalJob.objects.order_by("pk").values_list(
                "status", "input_manifest"
            )
        )
        self.assertEqual(before, after)
