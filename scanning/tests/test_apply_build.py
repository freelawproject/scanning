"""Tests for the build phase of the page edit apply (issue #224).

The build runs as queued work: the trigger on the collect tick, the
worker behind ``QueuedAction.APPLY_PAGE_EDITS``, the shards it cuts and
the rows it creates. S3 is stood in for by patches that record what
would be uploaded, so the tests see the keys and never the bucket.
"""

from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone

from scanning import apply, bitonal, dots_mocr, jobs, services, yolo
from scanning.models import (
    ApplyRun,
    ExternalJob,
    JobEngine,
    JobStage,
    JobStatus,
    PageEdit,
    QueuedAction,
    Scan,
    Status,
)
from scanning.tests.test_apply import (
    MEDIA_ROOT,
    ApplyTestCase,
    pdf_bytes,
    png_bytes,
)

ORIGINAL_KEY = "processing/1/a/1/1/vol.original.pdf"


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class BuildTestCase(ApplyTestCase):
    """A DONE scan, an S3 that records uploads, and open gates."""

    def setUp(self):
        super().setUp()
        Scan.objects.filter(pk=self.scan.pk).update(
            status=Status.PAGE_COMPLETENESS_REVIEW_DONE
        )
        self.scan.refresh_from_db()
        self.uploads: dict[str, object] = {}
        self.present: set[str] = set()

        def upload_file(key, path, content_type):
            self.uploads[key] = path.stat().st_size
            self.present.add(key)
            return True

        def upload_json(key, data):
            self.uploads[key] = data
            return True

        for target, side in (
            ("scanning.s3_sync.s3_active", lambda: True),
            ("scanning.s3_sync.upload_file_object", upload_file),
            ("scanning.s3_sync.upload_json_object", upload_json),
            (
                "scanning.s3_sync.object_exists",
                lambda key: key in self.present,
            ),
            ("scanning.s3_sync.object_size", lambda key: 512),
            ("scanning.s3_sync.s3_original_key", lambda scan: ORIGINAL_KEY),
            ("scanning.services._can_convert", lambda pk, manifest: True),
            ("scanning.services._can_analyze", lambda pk, manifest: True),
            ("scanning.yolo.enabled", lambda: True),
        ):
            patcher = patch(target, side_effect=side)
            patcher.start()
            self.addCleanup(patcher.stop)
        # The worker closes the connections for the daemon process; in a
        # test that would tear down the transaction the test runs in.
        closed = patch("django.db.connections.close_all")
        closed.start()
        self.addCleanup(closed.stop)

    def three_edits(self):
        """Write one edit of each shard kind, plus a deletion.

        :returns: ``(rotate, replace, insert)`` rows.
        """
        self.edit(PageEdit.Kind.DELETE_PAGE, pdf_page=2)
        turn = self.edit(PageEdit.Kind.ROTATE_PAGE, pdf_page=3, value="90")
        swap = self.upload_edit(
            PageEdit.Kind.REPLACE_PAGE, "r.png", png_bytes(), pdf_page=4
        )
        leaf = self.upload_edit(
            PageEdit.Kind.INSERT_PAGE,
            "leaf.pdf",
            pdf_bytes(2),
            anchor_pdf_page=5,
        )
        return turn, swap, leaf

    def rows(self, run, stage):
        """Return one stage's rows of a run, in shard order.

        :param run: The apply run.
        :param stage: A ``JobStage`` value.
        :returns: The rows.
        """
        return list(run.jobs.filter(stage=stage).order_by("shard_index"))


class TestBuildRun(BuildTestCase):
    """Phase 1."""

    def test_a_volume_with_no_edit_aliases_the_original(self):
        number = self.edit(PageEdit.Kind.SET_NUMBER, pdf_page=2, value="12")

        run = apply.build_run(self.scan)

        self.assertTrue(run.is_built)
        self.assertEqual(run.number, 1)
        self.assertEqual(run.final_pdf_key, ORIGINAL_KEY)
        self.assertEqual(run.edit_ids, [])
        self.assertEqual(run.page_map["final_page_count"], self.PAGES)
        self.assertEqual(run.jobs.count(), 0)
        self.assertEqual(
            list(self.uploads),
            [f"{apply.run_prefix(self.scan, run)}page_map.json"],
        )
        number.refresh_from_db()
        self.assertEqual(number.applied_run, run)
        self.assertIsNotNone(number.applied_at)
        self.assertEqual(run.source_fingerprint, self.scan.source_fingerprint)
        self.scan.refresh_from_db()
        self.assertEqual(self.scan.source_fingerprint, "100:6")

    def test_edits_make_shards_rows_and_a_final_pdf(self):
        turn, swap, leaf = self.three_edits()

        run = apply.build_run(self.scan)

        prefix = apply.run_prefix(self.scan, run)
        self.assertEqual(run.final_pdf_key, f"{prefix}final.pdf")
        self.assertIn(f"{prefix}page_map.json", self.uploads)
        for edit in (turn, swap, leaf):
            self.assertIn(apply.page_shard_key(self.scan, edit), self.uploads)
        # 6 pages, one deleted, two inserted.
        self.assertEqual(run.page_map["final_page_count"], 7)
        self.assertEqual(run.page_map["deleted_pages"], [2])

        for stage in (JobStage.CONVERT, JobStage.ANALYZE, JobStage.DETECT):
            with self.subTest(stage=stage):
                rows = self.rows(run, stage)
                self.assertEqual(len(rows), 3)
                self.assertEqual(
                    [row.input_key for row in rows],
                    [
                        apply.page_shard_key(self.scan, e)
                        for e in (turn, swap, leaf)
                    ],
                )
                self.assertEqual(
                    [row.input_manifest["page_count"] for row in rows],
                    [1, 1, 2],
                )
                self.assertEqual(
                    [row.input_manifest["edit_id"] for row in rows],
                    [turn.pk, swap.pk, leaf.pk],
                )
                self.assertTrue(all(row.apply_run == run for row in rows))
                self.assertTrue(
                    all(row.status == JobStatus.PENDING for row in rows)
                )

        for edit in (turn, swap, leaf):
            edit.refresh_from_db()
            self.assertEqual(edit.applied_run, run)
        self.assertEqual(
            sorted(run.edit_ids),
            sorted(
                e.pk
                for e in self.scan.page_edits.filter(
                    kind__in=PageEdit.STRUCTURAL_KINDS
                )
            ),
        )

    def test_the_volume_readers_ignore_the_apply_rows(self):
        self.three_edits()
        run = apply.build_run(self.scan)
        run.jobs.update(status=JobStatus.COMPLETED, result_key="r")

        self.assertEqual(
            jobs.live_run(self.scan, JobStage.ANALYZE, JobEngine.DOTS_MOCR), []
        )
        self.assertIsNone(dots_mocr.run_summary(self.scan))
        self.assertIsNone(yolo.run_summary(self.scan))
        self.assertEqual(dots_mocr.finish_ready_runs(), 0)
        self.assertEqual(yolo.finish_ready_runs(), 0)
        self.assertEqual(dots_mocr.apply_ready_runs(), 0)
        self.assertEqual(yolo.queue_ready_runs(), 0)
        Scan.objects.filter(pk=self.scan.pk).update(status=Status.AWAITING)
        self.assertEqual(bitonal.finish_ready_scans(), 0)
        self.assertEqual(
            len(
                jobs.live_run(
                    self.scan, JobStage.ANALYZE, JobEngine.DOTS_MOCR, run
                )
            ),
            3,
        )

    def test_the_result_keys_live_under_the_run(self):
        self.three_edits()
        run = apply.build_run(self.scan)
        row = self.rows(run, JobStage.CONVERT)[0]

        self.assertTrue(
            jobs.s3_sync.s3_job_attempt_key(row, ".pdf").startswith(
                f"{apply.run_prefix(self.scan, run)}convert/bitonal/"
            )
        )

    def test_a_second_build_reuses_the_shards_and_the_paid_results(self):
        turn, swap, leaf = self.three_edits()
        first = apply.build_run(self.scan)
        first.jobs.update(
            status=JobStatus.COMPLETED,
            result_key="paid",
            completed_at=timezone.now(),
        )
        self.present.add("paid")
        uploads_before = dict(self.uploads)
        # A reopen, a new deletion, a new approval.
        apply.supersede_runs(self.scan, "reopened")
        self.edit(PageEdit.Kind.DELETE_PAGE, pdf_page=6)

        second = apply.build_run(self.scan)

        self.assertEqual(second.number, 2)
        self.assertNotEqual(second.pk, first.pk)
        new_uploads = set(self.uploads) - set(uploads_before)
        self.assertEqual(
            new_uploads,
            {
                f"{apply.run_prefix(self.scan, second)}final.pdf",
                f"{apply.run_prefix(self.scan, second)}page_map.json",
            },
        )
        # The supersede left the paid rows alone, so the carry found them.
        self.assertEqual(
            set(first.jobs.values_list("status", flat=True)),
            {JobStatus.COMPLETED},
        )
        rows = list(second.jobs.all())
        self.assertEqual(len(rows), 9)
        self.assertTrue(all(row.status == JobStatus.COMPLETED for row in rows))
        self.assertTrue(
            all(row.provider_meta.get("carried_from") for row in rows)
        )
        self.assertEqual(second.page_map["deleted_pages"], [2, 6])

    def test_a_changed_edit_set_supersedes_the_built_run(self):
        first = apply.build_run(self.scan)
        # A built run owes its glue next; a changed edit set outranks it.
        self.assertEqual(apply.phase_due(self.scan), "glue")

        self.edit(PageEdit.Kind.DELETE_PAGE, pdf_page=1)

        self.assertEqual(apply.phase_due(self.scan), "build")
        second = apply.build_run(self.scan)
        first.refresh_from_db()
        self.assertIsNotNone(first.superseded_at)
        self.assertEqual(second.number, 2)
        self.assertEqual(apply.current_run(self.scan), second)

    def test_a_failed_build_counts_an_attempt_and_says_so(self):
        with patch("scanning.s3_sync.upload_json_object", return_value=False):
            with self.assertRaises(apply.ApplyError) as caught:
                apply.build_run(self.scan)

        self.assertIn("runs again by itself", str(caught.exception))
        run = apply.current_run(self.scan)
        self.assertFalse(run.is_built)
        self.assertEqual(run.attempts, 1)
        self.assertIn("page map", run.last_error)
        self.assertEqual(apply.phase_due(self.scan), "build")

    def test_spent_attempts_stop_the_trigger(self):
        run = ApplyRun.objects.create(
            scan=self.scan, number=1, attempts=apply.APPLY_MAX_ATTEMPTS
        )

        self.assertIsNone(apply.phase_due(self.scan))
        self.assertEqual(apply.queue_ready_scans(), 0)
        self.assertTrue(apply.run_state(self.scan, run)["failed"])

    def test_the_bar_state_counts_the_rows(self):
        self.three_edits()
        run = apply.build_run(self.scan)
        self.rows(run, JobStage.CONVERT)[0].__class__.objects.filter(
            pk=self.rows(run, JobStage.CONVERT)[0].pk
        ).update(status=JobStatus.COMPLETED)

        state = apply.run_state(self.scan)

        self.assertEqual(state["label"], "a1")
        self.assertEqual(state["open"], 8)
        self.assertIn("1 of 9", state["message"])
        self.assertFalse(state["failed"])

    def test_a_dead_row_holds_the_run(self):
        self.three_edits()
        run = apply.build_run(self.scan)
        run.jobs.update(status=JobStatus.COMPLETED)
        ExternalJob.objects.filter(
            pk=self.rows(run, JobStage.CONVERT)[0].pk
        ).update(status=JobStatus.FAILED, error_code="CONVERSION_TIMEOUT")

        self.assertIsNone(apply.phase_due(self.scan))
        state = apply.run_state(self.scan)
        self.assertTrue(state["failed"])
        self.assertIn("CONVERSION_TIMEOUT", state["message"])


class TestTriggerAndWorker(BuildTestCase):
    """The tick queues; the daemon claims and parks."""

    def test_an_approved_scan_is_queued_once(self):
        self.assertEqual(apply.queue_ready_scans(), 1)
        self.scan.refresh_from_db()
        self.assertEqual(self.scan.status, Status.QUEUED)
        self.assertEqual(
            self.scan.queued_action, QueuedAction.APPLY_PAGE_EDITS
        )
        self.assertEqual(apply.queue_ready_scans(), 0)

    def test_a_scan_still_in_review_is_not_queued(self):
        Scan.objects.filter(pk=self.scan.pk).update(
            status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )
        self.assertEqual(apply.queue_ready_scans(), 0)

    def test_the_trigger_does_not_build(self):
        with patch.object(apply, "build_run") as build:
            apply.queue_ready_scans()
        build.assert_not_called()

    def test_the_worker_builds_and_parks_the_scan_back_in_done(self):
        Scan.objects.filter(pk=self.scan.pk).update(status=Status.PROCESSING)

        services.run_apply_page_edits(self.scan.pk)

        self.scan.refresh_from_db()
        self.assertEqual(
            self.scan.status, Status.PAGE_COMPLETENESS_REVIEW_DONE
        )
        run = apply.current_run(self.scan)
        self.assertTrue(run.is_built)
        self.assertIn("Corrected volume", self.scan.progress_message)

    def test_a_lost_claim_supersedes_the_run(self):
        # The daemon's shutdown re-queued the scan while it built.
        Scan.objects.filter(pk=self.scan.pk).update(status=Status.QUEUED)

        services.run_apply_page_edits(self.scan.pk)

        self.scan.refresh_from_db()
        self.assertEqual(self.scan.status, Status.QUEUED)
        run = apply.latest_run(self.scan)
        self.assertIsNotNone(run.superseded_at)
        self.assertIsNone(apply.current_run(self.scan))

    def test_a_failed_build_parks_with_the_reason_and_never_errors(self):
        Scan.objects.filter(pk=self.scan.pk).update(status=Status.PROCESSING)

        with patch("scanning.s3_sync.upload_json_object", return_value=False):
            services.run_apply_page_edits(self.scan.pk)

        self.scan.refresh_from_db()
        self.assertEqual(
            self.scan.status, Status.PAGE_COMPLETENESS_REVIEW_DONE
        )
        self.assertIn("failed", self.scan.progress_message)
        self.assertIn("runs again by itself", self.scan.progress_message)
        # The trigger picks it up again.
        self.assertEqual(apply.queue_ready_scans(), 1)

    def test_a_scan_with_a_glued_run_is_left_alone(self):
        run = apply.build_run(self.scan)
        ApplyRun.objects.filter(pk=run.pk).update(
            bitonal_key="b",
            ocr_key="o",
            printed_pages_key="p",
            detections_key="d",
        )

        self.assertEqual(apply.queue_ready_scans(), 0)
