"""Tests for the Mistral OCR stage (issue #191).

What is tested here is what Mistral does *differently* from the two
RunPod engines: the render the daemon does itself, a wave that uploads
and creates a batch one shard per tick, a sweep that polls a batch and
stores its output whole, and a cancel that deletes what it uploaded.
The shared lifecycle -- the claim, the compare-and-swap, the retry
ledger, the carry -- is covered in ``test_jobs.py`` and
``test_dots_mocr.py``.

No HTTP and no S3: ``mistral_client`` and the S3 helpers are patched.
The render is real, over a synthetic PDF.
"""

import io
import json
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import fitz
from botocore.exceptions import ClientError
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from scanning import jobs, mistral_client, mistral_ocr, views_process
from scanning.factories import ScanFactory
from scanning.models import (
    ExternalJob,
    JobEngine,
    JobProvider,
    JobStage,
    JobStatus,
    Status,
)
from scanning.tests.test_jobs import make_manifest
from scanning.tests.test_views import ScanningTestCase

MISTRAL = {
    "MISTRAL_API_KEY": "key-1",
    "MISTRAL_MODEL": "mistral-ocr-latest",
    # The other providers off, so a tick exercises this wave alone.
    "DOTS_MOCR_ENABLED": False,
    "YOLO_ENABLED": False,
    "RUNPOD_ENABLED": False,
    "DOCTOR_ENABLED": False,
}


def ready():
    """Lift the redacted-source lock (#191, #206) for a test or a class.

    A factory rather than one shared patcher: a class decorator wraps
    the test methods alone, so a class whose ``setUp`` creates rows
    lifts the lock there instead, with ``self.enterContext(ready())``.
    What the lock itself does is tested in ``TestSwitchedOff``.

    :returns: The patcher.
    """
    return patch.object(mistral_ocr, "REDACTED_SOURCE_READY", True)


def on_request():
    """Lift the start-button lock (#191) for a test or a class.

    :returns: The patcher.
    """
    return patch.object(
        views_process, "MISTRAL_START_ON_REQUEST_ENABLED", True
    )


def extract_jobs(scan):
    """Return a scan's Mistral rows in shard order.

    :param scan: The scan to look up.
    :returns: Its rows.
    :rtype: list[ExternalJob]
    """
    return list(
        ExternalJob.objects.filter(
            scan=scan,
            stage=JobStage.EXTRACT,
            engine=JobEngine.MISTRAL_OCR,
        ).order_by("shard_index")
    )


def write_pdf(path: Path, pages: int = 2, size=(612, 792)) -> None:
    """Write a PDF of ``pages`` white pages with a black box on each.

    :param path: Where to write it.
    :param pages: How many pages.
    :param size: Page size in points; letter by default.
    """
    with fitz.open() as doc:
        for index in range(pages):
            page = doc.new_page(width=size[0], height=size[1])
            page.insert_text((72, 72), f"Page {index + 1}", fontsize=12)
            # A box in the top-left quarter, so a test can tell a
            # rendered page from a blank one.
            page.draw_rect(
                fitz.Rect(
                    size[0] * 0.1, size[1] * 0.1, size[0] * 0.4, size[1] * 0.4
                ),
                fill=(0, 0, 0),
                width=0,
            )
        doc.save(str(path))


def png_image(data: bytes) -> Image.Image:
    """Open PNG bytes as an image."""
    return Image.open(io.BytesIO(data)).convert("RGB")


# ── switches ────────────────────────────────────────────────────────
class TestEnabled(ScanningTestCase):
    @override_settings(**MISTRAL)
    def test_on_with_a_key(self):
        self.assertTrue(mistral_ocr.enabled())

    @override_settings(**{**MISTRAL, "MISTRAL_API_KEY": ""})
    def test_off_without_one(self):
        self.assertFalse(mistral_ocr.enabled())

    @override_settings(**MISTRAL)
    def test_the_other_engines_are_off_in_this_fixture(self):
        from scanning import dots_mocr, yolo

        self.assertFalse(dots_mocr.enabled())
        self.assertFalse(yolo.enabled())


# ── the rows ────────────────────────────────────────────────────────
@ready()
class TestEnsureExtractJobs(ScanningTestCase):
    """One row per shard, at EXTRACT/MISTRAL_OCR/MISTRAL."""

    def test_one_row_per_shard_at_extract_mistral(self):
        scan = ScanFactory()
        created = mistral_ocr.ensure_extract_jobs(
            scan, make_manifest(shard_count=2, pages_per_shard=10)
        )

        self.assertEqual(len(created), 2)
        for index, job in enumerate(created):
            self.assertEqual(job.stage, JobStage.EXTRACT)
            self.assertEqual(job.engine, JobEngine.MISTRAL_OCR)
            self.assertEqual(job.provider, JobProvider.MISTRAL)
            self.assertEqual(job.status, JobStatus.PENDING)
            self.assertIsNone(job.opinion)
            self.assertEqual(job.shard_index, index)
            self.assertEqual(job.input_manifest["from_page"], index * 10)
            self.assertIn("shards/", job.input_key)
            self.assertNotIn("bitonal", job.input_key)

    def test_a_second_call_reuses_the_run(self):
        scan = ScanFactory()
        manifest = make_manifest(shard_count=2)
        first = mistral_ocr.ensure_extract_jobs(scan, manifest)
        second = mistral_ocr.ensure_extract_jobs(scan, manifest)
        self.assertEqual([r.pk for r in first], [r.pk for r in second])

    def test_an_unchanged_shard_is_carried_into_a_replacement_run(self):
        # The results are kept, so a run replaced for one dead shard
        # re-pays that shard alone.
        scan = ScanFactory()
        manifest = make_manifest(shard_count=2, pages_per_shard=10)
        rows = mistral_ocr.ensure_extract_jobs(scan, manifest)
        ExternalJob.objects.filter(pk=rows[0].pk).update(
            status=JobStatus.CONSUMED,
            result_key="jobs/extract/mistral_ocr/r1-s0-a1.json",
            completed_at=timezone.now(),
        )
        ExternalJob.objects.filter(pk=rows[1].pk).update(
            status=JobStatus.FAILED
        )

        with (
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch("scanning.s3_sync.object_exists", return_value=True),
        ):
            fresh = mistral_ocr.ensure_extract_jobs(scan, manifest)

        self.assertEqual(fresh[0].run, 2)
        self.assertEqual(fresh[0].status, JobStatus.COMPLETED)
        self.assertEqual(
            fresh[0].result_key, "jobs/extract/mistral_ocr/r1-s0-a1.json"
        )
        self.assertEqual(fresh[0].external_id, "")
        self.assertEqual(fresh[1].status, JobStatus.PENDING)

    def test_a_result_with_a_hole_is_not_carried(self):
        scan = ScanFactory()
        manifest = make_manifest(shard_count=1, pages_per_shard=2)
        (row,) = mistral_ocr.ensure_extract_jobs(scan, manifest)
        ExternalJob.objects.filter(pk=row.pk).update(
            status=JobStatus.CONSUMED,
            result_key="jobs/extract/mistral_ocr/r1-s0-a1.json",
            provider_meta={"output": {"failed_pages": [1]}},
        )
        with (
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch("scanning.s3_sync.object_exists", return_value=True),
        ):
            fresh = mistral_ocr.ensure_extract_jobs(
                scan, manifest, force_new_run=True
            )
        self.assertEqual(fresh[0].run, 2)
        self.assertEqual(fresh[0].status, JobStatus.PENDING)

    def test_a_stable_hole_is_not_carried_either(self):
        # The stable-hole rule of #238 trusts a deterministic worker. A
        # Mistral batch line can fail from a transient fault, so two
        # runs with the same hole are two unlucky runs, not an answer.
        scan = ScanFactory()
        manifest = make_manifest(shard_count=1, pages_per_shard=2)
        for run in (1, 2):
            rows = mistral_ocr.ensure_extract_jobs(
                scan, manifest, force_new_run=run == 2
            )
            ExternalJob.objects.filter(pk=rows[0].pk).update(
                status=JobStatus.CONSUMED,
                result_key=f"jobs/extract/mistral_ocr/r{run}-s0-a1.json",
                provider_meta={"output": {"failed_pages": [1]}},
            )
        row = extract_jobs(scan)[-1]
        self.assertTrue(jobs.hole_is_stable(row))

        with (
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch("scanning.s3_sync.object_exists", return_value=True),
        ):
            fresh = mistral_ocr.ensure_extract_jobs(
                scan, manifest, force_new_run=True
            )
        self.assertEqual(fresh[0].run, 3)
        self.assertEqual(fresh[0].status, JobStatus.PENDING)


# ── the render ──────────────────────────────────────────────────────
class TestRender(ScanningTestCase):
    """The ensemble's canonical render: 1700x2200 RGB, nothing else."""

    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="mistral-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_a_letter_page_is_1700_by_2200_rgb(self):
        pdf = self.tmp / "letter.pdf"
        write_pdf(pdf, pages=1)
        with fitz.open(str(pdf)) as doc:
            png = mistral_ocr.render_page(doc[0])
        img = png_image(png)
        self.assertEqual(img.size, (1700, 2200))
        # The box the fixture draws, and white paper beside it: no
        # grey conversion and no threshold happened.
        self.assertEqual(img.getpixel((400, 400)), (0, 0, 0))
        self.assertEqual(img.getpixel((1600, 2100)), (255, 255, 255))

    def test_another_page_size_is_resized_to_the_canonical_size(self):
        # A4 is 595x842 points. The branch resizes every render to
        # 1700x2200, so every bbox of every engine lives in one space.
        pdf = self.tmp / "a4.pdf"
        write_pdf(pdf, pages=1, size=(595, 842))
        with fitz.open(str(pdf)) as doc:
            png = mistral_ocr.render_page(doc[0])
        img = png_image(png)
        self.assertEqual(img.size, (1700, 2200))
        self.assertEqual(img.getpixel((400, 400)), (0, 0, 0))
        self.assertEqual(img.getpixel((1600, 2100)), (255, 255, 255))

    def test_shard_pages_are_rendered_in_order(self):
        pdf = self.tmp / "shard.pdf"
        write_pdf(pdf, pages=3)
        pages = list(mistral_ocr.render_shard_pages(pdf))
        self.assertEqual([no for no, _ in pages], [0, 1, 2])
        for _, png in pages:
            self.assertEqual(png_image(png).size, (1700, 2200))


# ── the deadline ────────────────────────────────────────────────────
@ready()
class TestClaimDeadline(ScanningTestCase):
    def test_the_first_claim_stamps_mistral_s_timeout_plus_slack(self):
        (job,) = mistral_ocr.ensure_extract_jobs(
            ScanFactory(), make_manifest(1)
        )
        now = timezone.now()
        fields = mistral_ocr.claim_deadline(job, now)
        self.assertEqual(fields["deadline"] - now, timedelta(hours=25))

    def test_a_re_claim_writes_nothing(self):
        (job,) = mistral_ocr.ensure_extract_jobs(
            ScanFactory(), make_manifest(1)
        )
        job.deadline = timezone.now()
        self.assertEqual(mistral_ocr.claim_deadline(job, timezone.now()), {})


# ── the wave ────────────────────────────────────────────────────────
@override_settings(**MISTRAL)
class TestSubmitWave(ScanningTestCase):
    """Render, upload, manifest, create -- and what each failure costs."""

    def setUp(self):
        super().setUp()
        self.enterContext(ready())
        self.tmp = Path(tempfile.mkdtemp(prefix="mistral-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.pdf = self.tmp / "shard.pdf"
        write_pdf(self.pdf, pages=2)
        self.scan = ScanFactory()
        self.rows = mistral_ocr.ensure_extract_jobs(
            self.scan, make_manifest(shard_count=1, pages_per_shard=2)
        )

    def _download(self, key, dest):
        shutil.copy(self.pdf, dest)

    def _tick(
        self,
        upload=("f0", "f1", "m1"),
        create="batch-1",
        download=None,
        upload_error=None,
        create_error=None,
    ):
        patches = [
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch(
                "scanning.s3_sync.download_object",
                side_effect=download or self._download,
            ),
            patch(
                "scanning.mistral_client.upload_file",
                side_effect=upload_error or list(upload),
            ),
            patch(
                "scanning.mistral_client.create_batch",
                side_effect=create_error,
                return_value=create,
            ),
            patch("scanning.mistral_client.delete_file"),
            patch("scanning.mistral_client.cancel_batch"),
        ]
        mocks = []
        for patcher in patches:
            mocks.append(patcher.start())
            self.addCleanup(patcher.stop)
        summary = jobs.submit_pending()
        return summary, {
            "download": mocks[1],
            "upload": mocks[2],
            "create": mocks[3],
            "delete": mocks[4],
            "cancel": mocks[5],
        }

    def test_a_pending_shard_is_rendered_uploaded_and_submitted(self):
        summary, mocks = self._tick()

        self.assertEqual(summary.submitted, 1)
        job = extract_jobs(self.scan)[0]
        self.assertEqual(job.status, JobStatus.SUBMITTED)
        self.assertEqual(job.external_id, "batch-1")
        self.assertEqual(job.provider_meta["files"], ["f0", "f1", "m1"])
        self.assertEqual(job.provider_meta["submission"]["page_count"], 2)
        self.assertEqual(job.deadline - job.submitted_at, timedelta(hours=25))
        self.assertTrue(job.result_key.endswith("r1-s0-a1.json"))
        self.assertIn("jobs/extract/mistral_ocr/", job.result_key)

        # Two page images as ``ocr`` files, one manifest as ``batch``.
        calls = mocks["upload"].call_args_list
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0].args[0], "p0.png")
        self.assertEqual(calls[0].args[2], "ocr")
        self.assertEqual(calls[1].args[0], "p1.png")
        self.assertEqual(calls[2].args[2], "batch")
        # The pages are the canonical render.
        self.assertEqual(png_image(calls[0].args[1]).size, (1700, 2200))
        # The manifest names the uploaded pages, the model and the
        # branch's two request options.
        lines = [
            json.loads(line) for line in calls[2].args[1].decode().splitlines()
        ]
        self.assertEqual([line["custom_id"] for line in lines], ["p0", "p1"])
        self.assertEqual(
            lines[0]["body"]["document"], {"type": "file", "file_id": "f0"}
        )
        self.assertEqual(lines[1]["body"]["document"]["file_id"], "f1")
        self.assertEqual(lines[0]["body"]["model"], "mistral-ocr-latest")
        self.assertTrue(lines[0]["body"]["include_blocks"])
        self.assertEqual(lines[0]["body"]["table_format"], "html")
        # The batch names the manifest and Mistral's own timeout.
        kwargs = mocks["create"].call_args.kwargs
        self.assertEqual(mocks["create"].call_args.args, ("m1",))
        self.assertEqual(kwargs["timeout_hours"], 24)
        self.assertEqual(kwargs["metadata"]["scan"], str(self.scan.pk))
        self.assertEqual(kwargs["metadata"]["job"], str(job.pk))
        mocks["delete"].assert_not_called()

    def test_the_scratch_dir_names_the_scan_and_is_removed(self):
        # The shard PDF is rendered in a system temp dir the #215 sweep
        # reclaims by prefix; the name carries the scan and the shard,
        # as the other stages' do, and a normal exit removes it.
        real = tempfile.TemporaryDirectory
        seen = {}

        def spy(*args, **kwargs):
            tmp = real(*args, **kwargs)
            seen["prefix"] = kwargs.get("prefix")
            seen["path"] = Path(tmp.name)
            return tmp

        job = extract_jobs(self.scan)[0]
        with patch("scanning.mistral_ocr.tempfile.TemporaryDirectory", spy):
            self._tick()

        self.assertEqual(
            seen["prefix"],
            f"{mistral_ocr.RENDER_TMP_PREFIX}{self.scan.pk}-s{job.shard_index}-",
        )
        self.assertFalse(seen["path"].exists())

    def test_the_scratch_dir_is_removed_on_a_failure_too(self):
        real = tempfile.TemporaryDirectory
        seen = {}

        def spy(*args, **kwargs):
            tmp = real(*args, **kwargs)
            seen["path"] = Path(tmp.name)
            return tmp

        with patch("scanning.mistral_ocr.tempfile.TemporaryDirectory", spy):
            self._tick(
                upload_error=[
                    mistral_client.MistralError("bad", "HTTP_400", 400)
                ]
            )
        self.assertFalse(seen["path"].exists())

    def test_a_row_may_override_the_model(self):
        job = extract_jobs(self.scan)[0]
        ExternalJob.objects.filter(pk=job.pk).update(
            input_manifest={**job.input_manifest, "model": "mistral-ocr-2512"}
        )
        _, mocks = self._tick()
        line = json.loads(
            mocks["upload"].call_args_list[2].args[1].decode().splitlines()[0]
        )
        self.assertEqual(line["body"]["model"], "mistral-ocr-2512")

    def test_no_key_sends_nothing(self):
        with override_settings(MISTRAL_API_KEY=""):
            summary, mocks = self._tick()
        self.assertEqual(summary.submitted, 0)
        mocks["upload"].assert_not_called()
        self.assertEqual(extract_jobs(self.scan)[0].status, JobStatus.PENDING)

    def test_a_rate_limit_defers_and_deletes_the_uploads(self):
        # The first page uploaded, the second was declined: nothing is
        # wrong with the job, so the attempt is intact and the orphan
        # file is deleted.
        summary, mocks = self._tick(
            upload_error=[
                "f0",
                mistral_client.MistralBusy("slow down", "RATE_LIMITED", 429),
            ]
        )
        self.assertEqual(summary.deferred, 1)
        job = extract_jobs(self.scan)[0]
        self.assertEqual(job.status, JobStatus.PENDING)
        self.assertEqual(job.attempt, 1)
        self.assertEqual(job.error_code, "RATE_LIMITED")
        mocks["delete"].assert_called_once_with("f0")
        mocks["create"].assert_not_called()

    def test_a_transient_create_failure_retries_and_deletes(self):
        summary, mocks = self._tick(
            create_error=mistral_client.MistralTransientError(
                "no answer", mistral_client.UNANSWERED_ERROR_CODE
            )
        )
        self.assertEqual(summary.retried, 1)
        job = extract_jobs(self.scan)[0]
        self.assertEqual(job.status, JobStatus.PENDING)
        self.assertEqual(job.attempt, 2)
        self.assertEqual(job.external_id, "")
        self.assertEqual(
            [c.args[0] for c in mocks["delete"].call_args_list],
            ["f0", "f1", "m1"],
        )

    def test_a_refusal_fails_the_row(self):
        summary, mocks = self._tick(
            create_error=mistral_client.MistralError("bad", "HTTP_400", 400)
        )
        self.assertEqual(summary.failed, 1)
        job = extract_jobs(self.scan)[0]
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertEqual(job.error_code, "HTTP_400")
        self.assertEqual(mocks["delete"].call_count, 3)

    def test_a_shard_that_will_not_download_is_retried(self):
        error = ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

        def failing(key, dest):
            raise error

        summary, mocks = self._tick(download=failing)
        self.assertEqual(summary.retried, 1)
        job = extract_jobs(self.scan)[0]
        self.assertEqual(job.status, JobStatus.PENDING)
        self.assertEqual(job.error_code, "INPUT_DOWNLOAD_FAILED")
        mocks["upload"].assert_not_called()

    def test_a_row_cancelled_mid_submission_cancels_its_batch(self):
        # The row is taken away while the thread works: the id has
        # nowhere to live, so the batch is cancelled and the files
        # deleted, or Mistral bills for output nobody will read. The
        # other writer is simulated at the compare-and-swap: the write
        # of the id matches nothing, as it would after a cancel.
        real_write = jobs._write

        def write(job, **fields):
            if "external_id" in fields:
                return False
            return real_write(job, **fields)

        with patch("scanning.jobs._write", side_effect=write):
            summary, mocks = self._tick()

        self.assertEqual(summary.skipped, 1)
        job = extract_jobs(self.scan)[0]
        self.assertEqual(job.external_id, "")
        mocks["cancel"].assert_called_once_with("batch-1")
        self.assertEqual(mocks["delete"].call_count, 3)

    def test_one_shard_per_tick(self):
        # The wave blocks the serial scheduler for as long as a shard
        # takes, so a tick renders and submits one shard, whatever the
        # in-flight room.
        scan = ScanFactory()
        mistral_ocr.ensure_extract_jobs(
            scan, make_manifest(shard_count=3, pages_per_shard=2)
        )
        summary, _ = self._tick(upload=["f"] * 12, create="batch-1")
        self.assertEqual(summary.submitted, 1)
        # The next tick takes the next shard: the per-tick cap is not
        # the in-flight cap, so a volume keeps draining while its
        # batches wait at Mistral.
        summary, _ = self._tick(upload=["f"] * 12, create="batch-2")
        self.assertEqual(summary.submitted, 1)
        statuses = [
            job.status
            for job in ExternalJob.objects.filter(engine=JobEngine.MISTRAL_OCR)
        ]
        self.assertEqual(statuses.count(JobStatus.SUBMITTED), 2)

    def test_the_in_flight_cap_holds_a_tick_back(self):
        scan = ScanFactory()
        mistral_ocr.ensure_extract_jobs(
            scan, make_manifest(shard_count=3, pages_per_shard=2)
        )
        with (
            patch.object(mistral_ocr, "MAX_CONCURRENCY", 2),
            patch.object(mistral_ocr, "MAX_SUBMITS_PER_TICK", 10),
        ):
            summary, _ = self._tick(upload=["f"] * 12, create="batch-1")
            self.assertEqual(summary.submitted, 2)
            summary, _ = self._tick(upload=["f"] * 12, create="batch-2")
        # Two in flight at a cap of two: nothing more this tick.
        self.assertEqual(summary.submitted, 0)


# ── the sweep ───────────────────────────────────────────────────────
@override_settings(**MISTRAL)
class TestSweep(ScanningTestCase):
    """Poll, progress, harvest, retry -- and a claim the daemon lost."""

    OUTPUT = [
        {
            "id": "a",
            "custom_id": "p0",
            "response": {
                "status_code": 200,
                "body": {
                    "pages": [
                        {
                            "index": 0,
                            "markdown": "# One",
                            "images": [{"id": "img"}],
                            "dimensions": {
                                "dpi": 200,
                                "height": 2200,
                                "width": 1700,
                            },
                            "blocks": [{"type": "text", "content": "One"}],
                        }
                    ],
                    "usage_info": {"pages_processed": 1},
                    "model": "mistral-ocr-2512",
                },
            },
            "error": None,
        },
        {"id": "b", "custom_id": "p1", "response": None, "error": {"m": "x"}},
    ]
    ERRORS = [{"id": "b", "custom_id": "p1", "error": {"message": "x"}}]

    def setUp(self):
        super().setUp()
        self.enterContext(ready())
        self.scan = ScanFactory()
        (self.job,) = mistral_ocr.ensure_extract_jobs(
            self.scan, make_manifest(shard_count=1, pages_per_shard=2)
        )
        now = timezone.now()
        ExternalJob.objects.filter(pk=self.job.pk).update(
            status=JobStatus.SUBMITTED,
            external_id="batch-1",
            result_key="processing/1/jobs/extract/mistral_ocr/r1-s0-a1.json",
            submitted_at=now - timedelta(minutes=5),
            deadline=now + timedelta(hours=25),
            provider_meta={"files": ["f0", "f1", "m1"]},
        )
        self.job.refresh_from_db()

    def _sweep(self, outcome, now=None, output=None, errors=None, put=True):
        def download(file_id):
            return {"out-1": output or [], "err-1": errors or []}[file_id]

        patches = [
            patch("scanning.mistral_client.poll_batch", return_value=outcome),
            patch(
                "scanning.mistral_client.download_lines", side_effect=download
            ),
            patch("scanning.s3_sync.upload_json_object", return_value=put),
            patch("scanning.mistral_client.delete_file"),
            patch("scanning.mistral_client.cancel_batch"),
        ]
        mocks = []
        for patcher in patches:
            mocks.append(patcher.start())
            self.addCleanup(patcher.stop)
        summary = jobs.sweep_jobs(now=now)
        self.job.refresh_from_db()
        return summary, {
            "poll": mocks[0],
            "download": mocks[1],
            "put": mocks[2],
            "delete": mocks[3],
            "cancel": mocks[4],
        }

    def test_queued_is_recorded_with_its_progress(self):
        summary, mocks = self._sweep(
            mistral_client.BatchOutcome(
                status=JobStatus.IN_QUEUE, provider_status="QUEUED", total=2
            )
        )
        self.assertEqual(summary.pending, 1)
        self.assertEqual(self.job.status, JobStatus.IN_QUEUE)
        self.assertEqual(self.job.provider_meta["progress"]["total"], 2)
        mocks["poll"].assert_called_once()
        self.assertEqual(mocks["poll"].call_args.args[0], "batch-1")

    def test_running_does_not_restamp_the_deadline(self):
        before = self.job.deadline
        self._sweep(
            mistral_client.BatchOutcome(
                status=JobStatus.IN_PROGRESS,
                provider_status="RUNNING",
                total=2,
                succeeded=1,
            )
        )
        self.assertEqual(self.job.status, JobStatus.IN_PROGRESS)
        self.assertEqual(self.job.deadline, before)
        self.assertEqual(self.job.provider_meta["progress"]["succeeded"], 1)

    def test_success_stores_the_output_whole_and_completes(self):
        outcome = mistral_client.BatchOutcome(
            status=JobStatus.COMPLETED,
            provider_status="SUCCESS",
            total=2,
            succeeded=1,
            failed=1,
            output_file="out-1",
            error_file="err-1",
            job={"id": "batch-1", "status": "SUCCESS"},
        )
        summary, mocks = self._sweep(
            outcome, output=self.OUTPUT, errors=self.ERRORS
        )

        self.assertEqual(summary.completed, 1)
        self.assertEqual(self.job.status, JobStatus.COMPLETED)
        self.assertEqual(self.job.provider_meta["output"]["failed_pages"], [1])
        self.assertEqual(self.job.provider_meta["output"]["page_count"], 2)
        self.assertEqual(self.job.provider_meta["confirmed_by"], "response")
        self.assertNotIn("files", self.job.provider_meta)
        self.assertTrue(jobs.has_unread_pages(self.job))

        key, document = mocks["put"].call_args.args
        self.assertEqual(key, self.job.result_key)
        self.assertEqual(document["action"], "extract")
        self.assertEqual(document["scan_pk"], self.scan.pk)
        self.assertEqual(document["result_key"], self.job.result_key)
        payload = document["payload"]
        # Verbatim: the lines as Mistral wrote them, images and
        # usage_info included; the batch object beside them.
        self.assertEqual(payload["output"], self.OUTPUT)
        self.assertEqual(payload["errors"], self.ERRORS)
        self.assertEqual(payload["batch"]["id"], "batch-1")
        self.assertEqual(payload["failed_pages"], [1])
        self.assertEqual(
            payload["render"],
            {"width": 1700, "height": 2200, "source": "original"},
        )
        # Every file at Mistral is deleted once the object is in S3.
        deleted = [c.args[0] for c in mocks["delete"].call_args_list]
        self.assertEqual(deleted, ["f0", "f1", "m1", "out-1", "err-1"])

    def test_a_page_no_line_names_is_a_hole(self):
        outcome = mistral_client.BatchOutcome(
            status=JobStatus.COMPLETED,
            provider_status="SUCCESS",
            output_file="out-1",
            job={},
        )
        self._sweep(outcome, output=self.OUTPUT[:1])
        self.assertEqual(self.job.status, JobStatus.COMPLETED)
        self.assertEqual(self.job.provider_meta["output"]["failed_pages"], [1])

    def test_a_lost_completion_still_deletes_the_output_files(self):
        # Another writer took the row between the poll and the
        # completion. Its cancel deletes the files the row names; the
        # two output files are named by nothing else, so the harvest
        # deletes them itself.
        outcome = mistral_client.BatchOutcome(
            status=JobStatus.COMPLETED,
            provider_status="SUCCESS",
            output_file="out-1",
            error_file="err-1",
            job={},
        )
        with patch("scanning.jobs._complete", return_value=False):
            summary, mocks = self._sweep(outcome, output=self.OUTPUT)
        self.assertEqual(summary.errors, 1)
        deleted = [c.args[0] for c in mocks["delete"].call_args_list]
        self.assertEqual(deleted, ["out-1", "err-1"])

    def test_a_failed_put_leaves_the_row_in_flight(self):
        outcome = mistral_client.BatchOutcome(
            status=JobStatus.COMPLETED,
            provider_status="SUCCESS",
            output_file="out-1",
            job={},
        )
        with self.assertLogs("scanning.mistral_ocr", "WARNING"):
            summary, mocks = self._sweep(
                outcome, output=self.OUTPUT, put=False
            )
        self.assertEqual(summary.errors, 1)
        self.assertEqual(self.job.status, JobStatus.SUBMITTED)
        self.assertEqual(self.job.provider_meta["files"], ["f0", "f1", "m1"])
        mocks["delete"].assert_not_called()

    def test_a_failed_download_leaves_the_row_in_flight(self):
        outcome = mistral_client.BatchOutcome(
            status=JobStatus.COMPLETED,
            provider_status="SUCCESS",
            output_file="out-1",
            job={},
        )
        with (
            patch(
                "scanning.mistral_client.download_lines",
                side_effect=mistral_client.MistralTransientError(
                    "x", "HTTP_503"
                ),
            ),
            patch("scanning.mistral_client.poll_batch", return_value=outcome),
            patch("scanning.s3_sync.upload_json_object") as put,
            self.assertLogs("scanning.mistral_ocr", "WARNING"),
        ):
            summary = jobs.sweep_jobs()
        self.job.refresh_from_db()
        self.assertEqual(summary.errors, 1)
        self.assertEqual(self.job.status, JobStatus.SUBMITTED)
        put.assert_not_called()

    def test_a_timeout_retries_cancels_and_deletes(self):
        summary, mocks = self._sweep(
            mistral_client.BatchOutcome(
                status=JobStatus.FAILED,
                provider_status="TIMEOUT_EXCEEDED",
                error_code="BATCH_TIMEOUT_EXCEEDED",
                error_message="too slow",
                retriable=True,
            )
        )
        self.assertEqual(summary.retried, 1)
        self.assertEqual(self.job.status, JobStatus.PENDING)
        self.assertEqual(self.job.attempt, 2)
        self.assertIsNone(self.job.deadline)
        self.assertEqual(self.job.external_id, "")
        mocks["cancel"].assert_called_once_with("batch-1")
        deleted = [c.args[0] for c in mocks["delete"].call_args_list]
        self.assertEqual(deleted, ["f0", "f1", "m1"])

    def test_out_of_attempts_fails_for_good(self):
        ExternalJob.objects.filter(pk=self.job.pk).update(attempt=2)
        summary, _ = self._sweep(
            mistral_client.BatchOutcome(
                status=JobStatus.FAILED,
                provider_status="FAILED",
                error_code="BATCH_FAILED",
                retriable=True,
            )
        )
        self.assertEqual(summary.failed, 1)
        self.assertEqual(self.job.status, JobStatus.FAILED)
        self.assertEqual(self.job.error_code, "BATCH_FAILED")

    def test_cancelled_upstream_is_terminal(self):
        summary, mocks = self._sweep(
            mistral_client.BatchOutcome(
                status=JobStatus.CANCELLED,
                provider_status="CANCELLED",
                error_code="CANCELLED_UPSTREAM",
            )
        )
        self.assertEqual(summary.failed, 1)
        self.assertEqual(self.job.status, JobStatus.FAILED)
        self.assertEqual(self.job.attempt, 1)
        self.assertEqual(mocks["delete"].call_count, 3)

    def test_a_missing_job_is_retried(self):
        summary, _ = self._sweep(
            mistral_client.BatchOutcome(
                status=JobStatus.EXPIRED,
                provider_status="MISSING",
                error_code="BATCH_MISSING",
                retriable=True,
            )
        )
        self.assertEqual(summary.retried, 1)
        self.assertEqual(self.job.status, JobStatus.PENDING)

    def test_no_answer_learns_nothing(self):
        summary, _ = self._sweep(mistral_client.BatchOutcome(status=None))
        self.assertEqual(summary.pending, 1)
        self.assertEqual(self.job.status, JobStatus.SUBMITTED)
        self.assertIsNotNone(self.job.last_polled_at)

    def test_no_answer_past_the_deadline_is_retried(self):
        summary, _ = self._sweep(
            mistral_client.BatchOutcome(status=None),
            now=self.job.deadline + timedelta(seconds=1),
        )
        self.assertEqual(summary.retried, 1)
        self.assertEqual(self.job.error_code, "DEADLINE_EXCEEDED")

    def test_a_claim_with_no_batch_id_is_a_lost_claim(self):
        # The scheduler is serial: a SUBMITTED row with no id at sweep
        # time is a claim the daemon died holding.
        ExternalJob.objects.filter(pk=self.job.pk).update(external_id="")
        summary, mocks = self._sweep(mistral_client.BatchOutcome(status=None))
        self.assertEqual(summary.retried, 1)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, JobStatus.PENDING)
        self.assertEqual(self.job.error_code, "LOST_CLAIM")
        mocks["poll"].assert_not_called()


# ── the start button ────────────────────────────────────────────────
@ready()
@on_request()
@override_settings(**MISTRAL)
class TestStartButton(ScanningTestCase):
    """Staff-only, like the dots.mocr button, and it writes rows only."""

    def setUp(self):
        super().setUp()
        self.staff = self.make_staff_user()
        self.scan = ScanFactory(
            page_count=20, status=Status.AWAITING_VALIDATION
        )
        self.url = reverse("start_mistral_ocr", kwargs={"pk": self.scan.pk})
        self.manifest = make_manifest(shard_count=2, pages_per_shard=10)

    _UNSET = object()

    def _committed(self, manifest=_UNSET, reason=""):
        return patch(
            "scanning.sharding.committed_manifest",
            return_value=(
                self.manifest if manifest is self._UNSET else manifest,
                reason,
            ),
        )

    def _press(self, user=None):
        self.client.force_login(user or self.staff)
        return self.client.post(self.url)

    def _messages(self, response):
        return [str(m) for m in response.wsgi_request._messages]

    def test_staff_press_creates_one_row_per_shard(self):
        with self._committed():
            response = self._press()
        self.assertRedirects(
            response,
            reverse("scan_process", kwargs={"pk": self.scan.pk}),
            fetch_redirect_response=False,
        )
        rows = extract_jobs(self.scan)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r.status for r in rows}, {JobStatus.PENDING})
        self.assertIn("Queued Mistral OCR for 2", self._messages(response)[0])

    def test_no_review_state_is_required(self):
        # The button gates on the two locks and the shard set, not on
        # a status: where the stage is called from is a separate
        # question. Any status a scan can hold is fine.
        for status in (
            Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
            Status.PAGE_COMPLETENESS_REVIEW_DONE,
            Status.APPROVED,
        ):
            scan = ScanFactory(page_count=20, status=status)
            self.client.force_login(self.staff)
            with self._committed():
                self.client.post(
                    reverse("start_mistral_ocr", kwargs={"pk": scan.pk})
                )
            self.assertEqual(len(extract_jobs(scan)), 2, status)

    def test_the_request_never_calls_mistral(self):
        with (
            self._committed(),
            patch("scanning.mistral_client.upload_file") as upload,
            patch("scanning.mistral_client.create_batch") as create,
        ):
            self._press()
        upload.assert_not_called()
        create.assert_not_called()

    def test_the_request_never_cuts_shards(self):
        with (
            self._committed(),
            patch("scanning.sharding.ensure_shards") as cut,
        ):
            self._press()
        cut.assert_not_called()

    def test_a_non_staff_user_is_refused(self):
        with self._committed():
            response = self._press(self.make_user())
        self.assertEqual(extract_jobs(self.scan), [])
        self.assertIn("Only staff", self._messages(response)[0])

    def test_an_anonymous_user_is_redirected_to_login(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

    def test_a_get_is_rejected(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_no_key_is_refused(self):
        with override_settings(MISTRAL_API_KEY=""), self._committed():
            response = self._press()
        self.assertEqual(extract_jobs(self.scan), [])
        self.assertIn("MISTRAL_API_KEY", self._messages(response)[0])

    def test_no_committed_shard_set_is_refused(self):
        with self._committed(manifest=None, reason="no shard set"):
            response = self._press()
        self.assertEqual(extract_jobs(self.scan), [])
        self.assertIn("no shard set", self._messages(response)[0])

    def test_a_second_press_while_a_run_is_open_is_refused(self):
        with self._committed():
            self._press()
            first = [r.pk for r in extract_jobs(self.scan)]
            response = self._press()
        self.assertEqual([r.pk for r in extract_jobs(self.scan)], first)
        self.assertIn("already going", self._messages(response)[-1])

    def test_a_press_after_a_finished_run_reuses_it(self):
        with self._committed():
            self._press()
            rows = extract_jobs(self.scan)
            ExternalJob.objects.filter(pk__in=[r.pk for r in rows]).update(
                status=JobStatus.CONSUMED
            )
            with (
                patch("scanning.s3_sync.s3_active", return_value=True),
                patch("scanning.s3_sync.object_exists", return_value=True),
            ):
                response = self._press()
        self.assertEqual(len(extract_jobs(self.scan)), 2)
        self.assertIn("already read", self._messages(response)[-1])

    def test_the_run_shows_on_the_process_page(self):
        with self._committed():
            self._press()
        self.client.force_login(self.staff)
        response = self.client.get(
            reverse("process_actions", kwargs={"pk": self.scan.pk})
        )
        self.assertIn("Mistral OCR running", response.json()["html"])

    def test_the_bar_still_offers_no_button_to_staff(self):
        # The two locks are lifted for this class, and the button is
        # still gone: it is commented out in the template, so nothing
        # a flag says can put it back. TestSwitchedOff covers the bar
        # with the locks down.
        url = reverse("process_actions", kwargs={"pk": self.scan.pk})
        self.client.force_login(self.staff)
        for step in (1, 2):
            self.assertNotIn(
                "Run Mistral OCR",
                self.client.get(f"{url}?step={step}").json()["html"],
            )


# ── the two locks ───────────────────────────────────────────────────
@override_settings(**MISTRAL)
class TestSwitchedOff(ScanningTestCase):
    """The stage is off until a redacted volume exists (#191, #206).

    Two locks, and each is tested on its own, because they fail
    differently on purpose. The button refuses a person, with a
    message. ``ensure_extract_jobs`` raises, because a caller that
    reached it skipped the button and must not quietly send
    unredacted pages to a third party.
    """

    def test_the_creator_refuses_and_writes_no_row(self):
        scan = ScanFactory()
        with self.assertRaises(mistral_ocr.UnredactedSource):
            mistral_ocr.ensure_extract_jobs(scan, make_manifest(2))
        self.assertEqual(extract_jobs(scan), [])

    def test_the_creator_names_the_flag_to_lift(self):
        with self.assertRaises(mistral_ocr.UnredactedSource) as caught:
            mistral_ocr.ensure_extract_jobs(ScanFactory(), make_manifest(1))
        self.assertIn("REDACTED_SOURCE_READY", str(caught.exception))

    def test_the_button_refuses_and_calls_nothing(self):
        staff = self.make_staff_user()
        scan = ScanFactory(page_count=20)
        self.client.force_login(staff)
        with patch("scanning.sharding.committed_manifest") as committed:
            response = self.client.post(
                reverse("start_mistral_ocr", kwargs={"pk": scan.pk})
            )
        # Refused before the shard set is even looked up.
        committed.assert_not_called()
        self.assertEqual(extract_jobs(scan), [])
        message = str(list(response.wsgi_request._messages)[0])
        self.assertIn("not available yet", message)
        self.assertIn("redacted", message)

    def test_a_non_staff_user_is_still_refused_first(self):
        # The staff gate stays ahead of the switch, so the message a
        # curator sees is about permission, not about a stage they
        # were never offered.
        self.client.force_login(self.make_user())
        response = self.client.post(
            reverse("start_mistral_ocr", kwargs={"pk": ScanFactory().pk})
        )
        self.assertIn(
            "Only staff", str(list(response.wsgi_request._messages)[0])
        )

    def test_the_bar_offers_no_button(self):
        staff = self.make_staff_user()
        scan = ScanFactory(page_count=20, status=Status.PENDING_REVIEW)
        self.client.force_login(staff)
        page = self.client.get(
            reverse("scan_process", kwargs={"pk": scan.pk})
        ).content.decode()
        self.assertNotIn("Run Mistral OCR", page)
        self.assertNotIn("start-mistral", page)
