"""Tests for the ExternalJob model.

Model-layer only: the two stage shapes and their keys, the status sets
the daemon is written against, deadlines, and attempt history. Nothing
here submits anything.
"""

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from scanning.factories import (
    ExternalJobFactory,
    OpinionScanFactory,
    ScanFactory,
)
from scanning.models import (
    ExternalJob,
    JobEngine,
    JobProvider,
    JobStage,
    JobStatus,
    OpinionScan,
)
from scanning.tests.test_views import ScanningTestCase


class TestVolumeLevelJobs(ScanningTestCase):
    """CONVERT, DETECT and ANALYZE, which run once over the whole volume."""

    def test_volume_job_needs_no_opinion(self):
        job = ExternalJobFactory(stage=JobStage.DETECT)

        self.assertIsNone(job.opinion)
        self.assertEqual(job.shard_count, 1)

    def test_volume_job_may_not_name_an_opinion(self):
        """A volume stage predates every opinion, so it cannot target one."""
        scan = ScanFactory()
        opinion = OpinionScanFactory(scan=scan)

        for stage in [JobStage.CONVERT, JobStage.DETECT, JobStage.ANALYZE]:
            with self.subTest(stage=stage):
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        ExternalJobFactory(
                            scan=scan, opinion=opinion, stage=stage
                        )

    def test_a_volume_pass_fans_out_into_shards(self):
        """Sharding is how a slow full-volume pass is made fast.

        Bitonal on doctor and dots.mocr on RunPod both split the volume
        into page ranges and run them at once, so the shards differ only
        by ``shard_index`` and all of them have to fit under one key.
        """
        scan = ScanFactory()
        for index in range(4):
            ExternalJobFactory(
                scan=scan,
                stage=JobStage.CONVERT,
                engine=JobEngine.BITONAL,
                provider=JobProvider.DOCTOR,
                shard_index=index,
                shard_count=4,
            )

        self.assertEqual(scan.jobs.filter(stage=JobStage.CONVERT).count(), 4)

    def test_two_shards_may_not_share_an_index(self):
        scan = ScanFactory()
        ExternalJobFactory(
            scan=scan,
            stage=JobStage.CONVERT,
            engine=JobEngine.BITONAL,
            provider=JobProvider.DOCTOR,
            shard_index=1,
            shard_count=4,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ExternalJobFactory(
                    scan=scan,
                    stage=JobStage.CONVERT,
                    engine=JobEngine.BITONAL,
                    provider=JobProvider.DOCTOR,
                    shard_index=1,
                    shard_count=4,
                )

    def test_one_row_per_stage_engine_and_run(self):
        scan = ScanFactory()
        ExternalJobFactory(scan=scan, stage=JobStage.DETECT)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ExternalJobFactory(scan=scan, stage=JobStage.DETECT)

    def test_a_new_run_reuses_the_key(self):
        scan = ScanFactory()
        ExternalJobFactory(scan=scan, stage=JobStage.DETECT, run=1)
        ExternalJobFactory(scan=scan, stage=JobStage.DETECT, run=2)

        self.assertEqual(scan.jobs.count(), 2)

    def test_shard_index_must_be_inside_the_count(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ExternalJobFactory(shard_index=4, shard_count=4)

    def test_run_must_be_positive(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ExternalJobFactory(run=0)

    def test_str_names_the_scan(self):
        job = ExternalJobFactory(stage=JobStage.DETECT)

        self.assertIn(f"scan {job.scan_id} detect/blackletter", str(job))


class TestOpinionLevelJobs(ScanningTestCase):
    """EXTRACT and TIEBREAK, which run once per opinion PDF."""

    def setUp(self):
        self.scan = ScanFactory()
        self.opinion = OpinionScanFactory(scan=self.scan, page_start=1)

    def test_opinion_job_carries_both_scan_and_opinion(self):
        """The scan is denormalized so the rollup needs no join."""
        job = ExternalJobFactory(
            scan=self.scan,
            opinion=self.opinion,
            stage=JobStage.EXTRACT,
            engine=JobEngine.DOTS_MOCR,
        )

        self.assertEqual(job.scan_id, self.scan.pk)
        self.assertEqual(job.opinion_id, self.opinion.pk)

    def test_extract_job_requires_an_opinion(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ExternalJobFactory(
                    scan=self.scan,
                    stage=JobStage.EXTRACT,
                    engine=JobEngine.DOTS_MOCR,
                )

    def test_tiebreak_job_requires_an_opinion(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ExternalJobFactory(
                    scan=self.scan,
                    stage=JobStage.TIEBREAK,
                    engine=JobEngine.LIGHTON_OCR,
                )

    def test_three_engines_read_one_opinion(self):
        for engine, provider in [
            (JobEngine.DOTS_MOCR, JobProvider.RUNPOD),
            (JobEngine.MISTRAL_OCR, JobProvider.MISTRAL),
            (JobEngine.SURYA, JobProvider.RUNPOD),
        ]:
            ExternalJobFactory(
                scan=self.scan,
                opinion=self.opinion,
                stage=JobStage.EXTRACT,
                engine=engine,
                provider=provider,
            )

        self.assertEqual(self.opinion.jobs.count(), 3)

    def test_one_engine_reads_many_opinions(self):
        """A volume's opinions each get their own row, keyed by opinion."""
        opinions = [
            OpinionScanFactory(scan=self.scan, page_start=start)
            for start in (1, 11, 21)
        ]
        for opinion in opinions:
            ExternalJobFactory(
                scan=self.scan,
                opinion=opinion,
                stage=JobStage.EXTRACT,
                engine=JobEngine.DOTS_MOCR,
            )

        self.assertEqual(
            self.scan.jobs.filter(stage=JobStage.EXTRACT).count(), 3
        )

    def test_one_row_per_opinion_engine_and_run(self):
        ExternalJobFactory(
            scan=self.scan,
            opinion=self.opinion,
            stage=JobStage.EXTRACT,
            engine=JobEngine.DOTS_MOCR,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ExternalJobFactory(
                    scan=self.scan,
                    opinion=self.opinion,
                    stage=JobStage.EXTRACT,
                    engine=JobEngine.DOTS_MOCR,
                )

    def test_two_opinions_do_not_collide_on_the_volume_key(self):
        """Opinion rows are keyed by opinion, not by shard_index.

        Without the conditional constraints, every opinion after the
        first would look like a duplicate of scan/stage/engine/run/0.
        """
        other = OpinionScanFactory(scan=self.scan, page_start=11)
        for opinion in (self.opinion, other):
            ExternalJobFactory(
                scan=self.scan,
                opinion=opinion,
                stage=JobStage.EXTRACT,
                engine=JobEngine.DOTS_MOCR,
                shard_index=0,
                shard_count=1,
            )

        self.assertEqual(self.scan.jobs.count(), 2)

    def test_opinion_must_belong_to_the_job_scan(self):
        stranger = OpinionScanFactory(scan=ScanFactory())
        job = ExternalJobFactory.build(
            scan=self.scan,
            opinion=stranger,
            stage=JobStage.EXTRACT,
            engine=JobEngine.DOTS_MOCR,
        )

        with self.assertRaises(ValidationError) as ctx:
            job.full_clean()
        self.assertIn("opinion", ctx.exception.message_dict)

    def test_regenerating_opinions_discards_their_jobs(self):
        """What ``run_generate_files`` does today, and its cost.

        It deletes and recreates a scan's OpinionScan rows, so the
        cascade takes the extraction jobs with them. Correct when the
        opinion really changed, expensive when it did not, which is
        issue #165.
        """
        ExternalJobFactory(
            scan=self.scan,
            opinion=self.opinion,
            stage=JobStage.EXTRACT,
            engine=JobEngine.DOTS_MOCR,
            status=JobStatus.CONSUMED,
        )

        OpinionScan.objects.filter(scan=self.scan).delete()

        self.assertEqual(ExternalJob.objects.count(), 0)

    def test_str_names_the_opinion(self):
        job = ExternalJobFactory(
            scan=self.scan,
            opinion=self.opinion,
            stage=JobStage.EXTRACT,
            engine=JobEngine.DOTS_MOCR,
        )

        self.assertIn(f"opinion {self.opinion.pk} extract/dots_mocr", str(job))

    def test_box_work_is_described_by_the_manifest(self):
        """A tiebreak read is a list of boxes, not a page range."""
        job = ExternalJobFactory(
            scan=self.scan,
            opinion=self.opinion,
            stage=JobStage.TIEBREAK,
            engine=JobEngine.LIGHTON_OCR,
            input_manifest={
                "crops": [
                    {
                        "key": "page_0007_84_132_1620_230",
                        "page_index": 7,
                        "bbox": [84, 132, 1620, 230],
                    }
                ]
            },
        )

        self.assertEqual(len(job.input_manifest["crops"]), 1)

    def test_input_provenance_survives_a_rename(self):
        """The FK says which opinion; these say which bytes.

        Opinion PDFs are renamed when pairing moves a boundary, so an
        extraction is only known to be current if the file it read still
        hashes the same.
        """
        job = ExternalJobFactory(
            scan=self.scan,
            opinion=self.opinion,
            stage=JobStage.EXTRACT,
            engine=JobEngine.DOTS_MOCR,
            input_key="opinions/a/218/redacted/a.218.0001-0010.pdf",
            input_hash="deadbeef",
        )

        self.assertTrue(job.input_key)
        self.assertEqual(job.input_hash, "deadbeef")


class TestExternalJobRuns(ScanningTestCase):
    """Run numbering, which is what a re-queue and a retry differ on."""

    def test_next_run_starts_at_one(self):
        scan = ScanFactory()

        self.assertEqual(
            ExternalJob.next_run(scan, JobStage.DETECT, JobEngine.BLACKLETTER),
            1,
        )

    def test_next_run_follows_the_highest_run(self):
        scan = ScanFactory()
        ExternalJobFactory(scan=scan, stage=JobStage.DETECT, run=1)
        ExternalJobFactory(scan=scan, stage=JobStage.DETECT, run=3)

        self.assertEqual(
            ExternalJob.next_run(scan, JobStage.DETECT, JobEngine.BLACKLETTER),
            4,
        )

    def test_next_run_is_per_stage(self):
        scan = ScanFactory()
        ExternalJobFactory(scan=scan, stage=JobStage.DETECT, run=5)

        self.assertEqual(
            ExternalJob.next_run(
                scan, JobStage.ANALYZE, JobEngine.BLACKLETTER
            ),
            1,
        )

    def test_next_run_is_per_engine(self):
        """One engine's re-run must not raise the other engine's run.

        Otherwise a stage barrier reading "rows at max(run)" stops
        seeing the engine that was not re-run, and declares the stage
        finished without its results.
        """
        scan = ScanFactory()
        opinion = OpinionScanFactory(scan=scan)
        for engine, provider in [
            (JobEngine.DOTS_MOCR, JobProvider.RUNPOD),
            (JobEngine.MISTRAL_OCR, JobProvider.MISTRAL),
        ]:
            ExternalJobFactory(
                scan=scan,
                opinion=opinion,
                stage=JobStage.EXTRACT,
                engine=engine,
                provider=provider,
                run=1,
            )

        self.assertEqual(
            ExternalJob.next_run(
                scan, JobStage.EXTRACT, JobEngine.DOTS_MOCR, opinion
            ),
            2,
        )
        self.assertEqual(
            ExternalJob.next_run(
                scan, JobStage.EXTRACT, JobEngine.MISTRAL_OCR, opinion
            ),
            2,
        )

    def test_next_run_is_per_opinion(self):
        """Re-reading one opinion must not renumber the other 299."""
        scan = ScanFactory()
        first = OpinionScanFactory(scan=scan, page_start=1)
        second = OpinionScanFactory(scan=scan, page_start=11)
        for opinion in (first, second):
            ExternalJobFactory(
                scan=scan,
                opinion=opinion,
                stage=JobStage.EXTRACT,
                engine=JobEngine.DOTS_MOCR,
                run=1,
            )
        ExternalJobFactory(
            scan=scan,
            opinion=first,
            stage=JobStage.EXTRACT,
            engine=JobEngine.DOTS_MOCR,
            run=2,
        )

        self.assertEqual(
            ExternalJob.next_run(
                scan, JobStage.EXTRACT, JobEngine.DOTS_MOCR, first
            ),
            3,
        )
        self.assertEqual(
            ExternalJob.next_run(
                scan, JobStage.EXTRACT, JobEngine.DOTS_MOCR, second
            ),
            2,
        )

    def test_next_run_is_stable_for_a_stage_wide_rerun(self):
        """Submitting engine after engine keeps them on the same run."""
        scan = ScanFactory()
        engines = [JobEngine.BLACKLETTER]
        for engine in engines:
            ExternalJobFactory(
                scan=scan, stage=JobStage.DETECT, engine=engine, run=1
            )

        runs = []
        for engine in engines:
            run = ExternalJob.next_run(scan, JobStage.DETECT, engine)
            ExternalJobFactory(
                scan=scan, stage=JobStage.DETECT, engine=engine, run=run
            )
            runs.append(run)

        self.assertEqual(runs, [2])


class TestExternalJobStatuses(ScanningTestCase):
    """The status sets the daemon is written against."""

    def test_completed_is_open_not_terminal(self):
        """A finished-but-unread job still needs the daemon's attention."""
        job = ExternalJobFactory(status=JobStatus.COMPLETED)

        self.assertTrue(job.is_open)
        self.assertFalse(job.is_terminal)

    def test_consumed_is_terminal(self):
        job = ExternalJobFactory(status=JobStatus.CONSUMED)

        self.assertFalse(job.is_open)
        self.assertTrue(job.is_terminal)

    def test_open_queryset_spans_pending_in_flight_and_completed(self):
        scan = ScanFactory()
        for index, status in enumerate(
            [
                JobStatus.PENDING,
                JobStatus.SUBMITTED,
                JobStatus.IN_QUEUE,
                JobStatus.IN_PROGRESS,
                JobStatus.COMPLETED,
                JobStatus.CONSUMED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
                JobStatus.EXPIRED,
            ]
        ):
            ExternalJobFactory(
                scan=scan, status=status, shard_index=index, shard_count=9
            )

        self.assertEqual(ExternalJob.objects.open().count(), 5)
        self.assertEqual(ExternalJob.objects.in_flight().count(), 3)
        self.assertEqual(ExternalJob.objects.terminal().count(), 4)

    def test_overdue_only_covers_in_flight_jobs(self):
        scan = ScanFactory()
        past = timezone.now() - timedelta(minutes=5)
        overdue = ExternalJobFactory(
            scan=scan,
            status=JobStatus.IN_PROGRESS,
            deadline=past,
            shard_index=0,
            shard_count=3,
        )
        # Completed: the deadline has passed but the work is done, so
        # cancelling it would throw away a harvestable result.
        ExternalJobFactory(
            scan=scan,
            status=JobStatus.COMPLETED,
            deadline=past,
            shard_index=1,
            shard_count=3,
        )
        # In flight with room to spare.
        ExternalJobFactory(
            scan=scan,
            status=JobStatus.IN_PROGRESS,
            deadline=timezone.now() + timedelta(minutes=5),
            shard_index=2,
            shard_count=3,
        )

        self.assertEqual(
            list(ExternalJob.objects.overdue().values_list("pk", flat=True)),
            [overdue.pk],
        )

    def test_deadlineless_job_is_never_overdue(self):
        job = ExternalJobFactory(status=JobStatus.IN_PROGRESS, deadline=None)

        self.assertFalse(job.is_overdue())
        self.assertEqual(ExternalJob.objects.overdue().count(), 0)

    def test_is_overdue_accepts_a_comparison_time(self):
        deadline = timezone.now()
        job = ExternalJobFactory(
            status=JobStatus.IN_PROGRESS, deadline=deadline
        )

        self.assertFalse(job.is_overdue(deadline - timedelta(seconds=1)))
        self.assertTrue(job.is_overdue(deadline + timedelta(seconds=1)))

    def test_bulk_update_still_stamps_date_modified(self):
        """The custom queryset must not drop AutoNowQuerySet's behaviour."""
        job = ExternalJobFactory(status=JobStatus.SUBMITTED)
        before = job.date_modified

        ExternalJob.objects.in_flight().update(status=JobStatus.IN_PROGRESS)
        job.refresh_from_db()

        self.assertEqual(job.status, JobStatus.IN_PROGRESS)
        self.assertGreater(job.date_modified, before)


class TestExternalJobResume(ScanningTestCase):
    """What a restarted daemon needs to reattach instead of resubmit."""

    def test_in_flight_job_keeps_its_provider_handle(self):
        scan = ScanFactory()
        job = ExternalJobFactory(
            scan=scan,
            status=JobStatus.IN_PROGRESS,
            external_id="a1b2c3",
            result_key="processing/1/jobs/detect/run1/shard_0000.a1.json",
            submitted_at=timezone.now(),
        )

        # What the daemon does on the tick after a restart: find the
        # rows it left behind, by state alone.
        recovered = ExternalJob.objects.filter(scan=scan).in_flight().first()

        self.assertEqual(recovered.pk, job.pk)
        self.assertEqual(recovered.external_id, "a1b2c3")
        self.assertTrue(recovered.result_key)

    def test_completed_job_is_recoverable_without_the_provider(self):
        """The result key is enough: head the object, harvest, move on."""
        job = ExternalJobFactory(
            status=JobStatus.COMPLETED,
            external_id="a1b2c3",
            result_key="processing/1/jobs/detect/run1/shard_0000.a1.json",
            completed_at=timezone.now(),
        )

        self.assertTrue(job.is_open)
        self.assertTrue(job.result_key)


class TestExternalJobAttempts(ScanningTestCase):
    """Retries mutate the row, so history has to be kept explicitly."""

    def test_attempt_defaults_to_one(self):
        job = ExternalJobFactory()

        self.assertEqual(job.attempt, 1)

    def test_attempt_distinguishes_resubmissions_of_one_row(self):
        """The result key is scoped by attempt, so it has to move.

        A resubmission keeps the same target, stage, engine, and run, so
        without this an abandoned worker's late upload lands on the key
        the new attempt is about to read.
        """
        job = ExternalJobFactory(attempt=1, run=1)

        job.push_attempt(save=False)
        job.attempt = 2
        job.save(update_fields=["attempt", "provider_meta"])
        job.refresh_from_db()

        self.assertEqual(job.attempt, 2)
        self.assertEqual(job.provider_meta["attempts"][0]["attempt"], 1)
        # Identity is otherwise untouched: this is the same unit of work.
        self.assertEqual(job.run, 1)
        self.assertEqual(job.shard_index, 0)

    def test_push_attempt_records_the_provider_handle(self):
        job = ExternalJobFactory(
            status=JobStatus.FAILED,
            external_id="job-abc",
            error_code="WORKER_ERROR",
            error_message="boom",
            result_key="jobs/detect/run1/shard_0000.a1.json",
            submitted_at=timezone.now(),
        )

        job.push_attempt()
        job.refresh_from_db()

        attempts = job.provider_meta["attempts"]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["external_id"], "job-abc")
        self.assertEqual(attempts[0]["status"], JobStatus.FAILED)
        self.assertEqual(attempts[0]["error_code"], "WORKER_ERROR")
        self.assertIsNotNone(attempts[0]["submitted_at"])
        self.assertIsNone(attempts[0]["completed_at"])

    def test_attempts_accumulate_across_retries(self):
        job = ExternalJobFactory(external_id="first")
        job.push_attempt()

        job.external_id = "second"
        job.retry_count = 1
        job.save(update_fields=["external_id", "retry_count"])
        job.push_attempt()
        job.refresh_from_db()

        self.assertEqual(
            [a["external_id"] for a in job.provider_meta["attempts"]],
            ["first", "second"],
        )

    def test_push_attempt_can_defer_the_write(self):
        job = ExternalJobFactory(external_id="job-abc")

        attempts = job.push_attempt(save=False)
        job.refresh_from_db()

        self.assertEqual(len(attempts), 1)
        self.assertEqual(job.provider_meta, {})

    def test_push_attempt_preserves_other_provider_meta(self):
        """Diagnostics share the column with the attempt history."""
        job = ExternalJobFactory(
            provider_meta={"cost": 0.031, "endpoint_id": "abc123"}
        )

        job.push_attempt()
        job.refresh_from_db()

        self.assertEqual(job.provider_meta["cost"], 0.031)
        self.assertEqual(job.provider_meta["endpoint_id"], "abc123")
        self.assertEqual(len(job.provider_meta["attempts"]), 1)
