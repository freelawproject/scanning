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
import re
import shutil
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import fitz
from botocore.exceptions import ClientError
from django.db import connections
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from scanning import jobs, locks, mistral_client, mistral_ocr, stopping
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


def _answer(script: list):
    """Pop the next scripted answer of a fake: raised if an exception,
    returned otherwise; the last one repeats."""
    answer = script.pop(0) if len(script) > 1 else script[0]
    if isinstance(answer, BaseException):
        raise answer
    return answer


MISTRAL = {
    "MISTRAL_API_KEY": "key-1",
    "MISTRAL_MODEL": "mistral-ocr-latest",
    # The other providers off, so a tick exercises this wave alone.
    "DOTS_MOCR_ENABLED": False,
    "YOLO_ENABLED": False,
    "RUNPOD_ENABLED": False,
    "DOCTOR_ENABLED": False,
}


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


def _page_of(name: str) -> int:
    """Return the shard-local page an uploaded page file is named for."""
    return int(re.search(r"-p(\d+)\.png$", name).group(1))


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

    def test_the_whiteout_paints_the_perpendiculars_out(self):
        """A page with one perpendicular drawn as print: a bar 50 by 6
        px at 200 dpi with a stem 12 px in. Painted out; without the
        whiteout the stem is in the PNG."""
        pdf = self.tmp / "perpendicular.pdf"
        black = (0, 0, 0)
        with fitz.open() as doc:
            page = doc.new_page(width=612, height=792)
            page.draw_rect(
                fitz.Rect(216, 360, 234, 362.2), color=black, fill=black
            )
            page.draw_rect(
                fitz.Rect(220.3, 351, 221.4, 360), color=black, fill=black
            )
            doc.save(str(pdf))

        pages = list(mistral_ocr.render_shard_pages(pdf, whiteout=True))

        painted = png_image(pages[0][1])
        self.assertEqual(painted.getpixel((613, 990)), (255, 255, 255))
        plain = png_image(next(mistral_ocr.render_shard_pages(pdf))[1])
        self.assertEqual(plain.getpixel((613, 990)), (0, 0, 0))

    def test_shard_pages_are_rendered_in_order(self):
        pdf = self.tmp / "shard.pdf"
        write_pdf(pdf, pages=3)
        pages = list(mistral_ocr.render_shard_pages(pdf))
        self.assertEqual([no for no, _ in pages], [0, 1, 2])
        for _, png in pages:
            self.assertEqual(png_image(png).size, (1700, 2200))


# ── the deadline ────────────────────────────────────────────────────
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
        workers=1,
        backoff=0.0,
        per_tick=None,
        cap=None,
        renders=None,
        delete_ok=True,
    ):
        # One upload thread unless a test asks for more: the
        # side-effect lists above answer the calls in the order they
        # are made, which only a serial pool keeps in page order. No
        # wait between two tries of a throttled page unless a test
        # asks for one. The settings override is scoped to the call,
        # so it nests inside a test's own override.
        overrides = {"MISTRAL_UPLOAD_WORKERS": workers}
        if per_tick is not None:
            overrides["MISTRAL_SUBMITS_PER_TICK"] = per_tick
        if cap is not None:
            overrides["MISTRAL_MAX_CONCURRENCY"] = cap
        if renders is not None:
            overrides["MISTRAL_RENDER_WORKERS"] = renders
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
            patch(
                "scanning.mistral_client.delete_file", return_value=delete_ok
            ),
            patch("scanning.mistral_client.cancel_batch"),
            patch.object(mistral_ocr, "UPLOAD_BACKOFF_SECONDS", backoff),
        ]
        mocks = []
        for patcher in patches:
            mocks.append(patcher.start())
            self.addCleanup(patcher.stop)
        with override_settings(**overrides):
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

        # Two page images as ``ocr`` files, one manifest as ``batch``,
        # every name under the attempt's prefix (#534).
        calls = mocks["upload"].call_args_list
        self.assertEqual(len(calls), 3)
        prefix = f"scan{self.scan.pk}-job{job.pk}-a1-"
        self.assertEqual(mistral_ocr.file_prefix(job), prefix)
        self.assertEqual(calls[0].args[0], f"{prefix}p0.png")
        self.assertEqual(calls[0].args[2], "ocr")
        self.assertEqual(calls[1].args[0], f"{prefix}p1.png")
        self.assertEqual(calls[2].args[0], f"{prefix}manifest.jsonl")
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

    def test_a_stop_mid_shard_hands_the_row_back_and_deletes_the_pages(self):
        # A shutdown while the pages upload (#534): the stop is read
        # before each page is handed to the pool, so the page that
        # asked for it may be joined by the one rendered meanwhile and
        # never by the manifest; every page that reached Mistral is
        # deleted, no batch is created, and the row goes back with its
        # attempt intact.
        self.addCleanup(stopping.reset)
        uploaded = []

        def upload(name, content, purpose):
            uploaded.append(name)
            stopping.request()
            return f"f{len(uploaded) - 1}"

        summary, mocks = self._tick(upload_error=upload)

        # The page rendered meanwhile is either never handed over or
        # starts in the pool and stops at once: one upload, no more.
        self.assertEqual(summary.deferred, 1)
        self.assertEqual(len(uploaded), 1)
        self.assertTrue(all(name.endswith(".png") for name in uploaded))
        mocks["create"].assert_not_called()
        self.assertEqual(
            sorted(c.args[0] for c in mocks["delete"].call_args_list),
            [f"f{i}" for i in range(len(uploaded))],
        )
        job = extract_jobs(self.scan)[0]
        self.assertEqual(job.status, JobStatus.PENDING)
        self.assertEqual(job.attempt, 1)
        self.assertEqual(job.error_code, mistral_ocr.STOPPED_ERROR_CODE)
        self.assertIsNone(job.submitted_at)

    def _six_page_shard(self):
        """Replace the two-page shard with a six-page one, so a pool of
        three has pages to overlap, and return its download stub."""
        ExternalJob.objects.filter(scan=self.scan).delete()
        pdf = self.tmp / "six.pdf"
        write_pdf(pdf, pages=6)
        mistral_ocr.ensure_extract_jobs(
            self.scan, make_manifest(shard_count=1, pages_per_shard=6)
        )
        return lambda key, dest: shutil.copy(pdf, dest)

    def test_the_pages_upload_in_parallel_and_the_manifest_names_each(self):
        # Six pages through three upload threads, each answering later
        # than the next, so they finish out of page order: the pool
        # overlapped them, the recorded ids are in page order, and the
        # manifest names each page's own file.
        download = self._six_page_shard()
        running = []
        most = [0]
        lock = threading.Lock()

        def upload(name, content, purpose):
            if name.endswith("manifest.jsonl"):
                return "m"
            page = _page_of(name)
            with lock:
                running.append(name)
                most[0] = max(most[0], len(running))
            time.sleep(0.05 * (6 - page))
            with lock:
                running.remove(name)
            return f"id-p{page}"

        summary, mocks = self._tick(
            download=download, upload_error=upload, workers=3
        )

        self.assertEqual(summary.submitted, 1)
        self.assertGreaterEqual(most[0], 2)
        job = extract_jobs(self.scan)[0]
        self.assertEqual(
            job.provider_meta["files"], [f"id-p{i}" for i in range(6)] + ["m"]
        )
        self.assertEqual(job.provider_meta["submission"]["page_count"], 6)
        manifest = mocks["upload"].call_args_list[-1]
        self.assertTrue(manifest.args[0].endswith("manifest.jsonl"))
        lines = [
            json.loads(line) for line in manifest.args[1].decode().splitlines()
        ]
        self.assertEqual(
            [
                (line["custom_id"], line["body"]["document"]["file_id"])
                for line in lines
            ],
            [(f"p{i}", f"id-p{i}") for i in range(6)],
        )
        mocks["delete"].assert_not_called()

    def test_a_failed_upload_deletes_every_page_that_landed(self):
        # One page of six is refused for good while the others are in
        # flight: the render stops at the next page, every page that
        # landed is deleted, none is orphaned, and the row is failed.
        download = self._six_page_shard()
        landed = []
        lock = threading.Lock()

        def upload(name, content, purpose):
            page = _page_of(name)
            if page == 2:
                raise mistral_client.MistralError("bad", "HTTP_400", 400)
            time.sleep(0.02)
            with lock:
                landed.append(f"id-p{page}")
            return f"id-p{page}"

        summary, mocks = self._tick(
            download=download, upload_error=upload, workers=3
        )

        self.assertEqual(summary.failed, 1)
        self.assertEqual(extract_jobs(self.scan)[0].status, JobStatus.FAILED)
        mocks["create"].assert_not_called()
        self.assertEqual(
            sorted(c.args[0] for c in mocks["delete"].call_args_list),
            sorted(landed),
        )
        # The render stopped: the pages after the failure were not all
        # handed over.
        self.assertLess(len(mocks["upload"].call_args_list), 6)

    def test_the_render_waits_behind_a_stalled_upload(self):
        # One upload thread, a backlog of two, and the first upload held
        # until the render has three pages out (one uploading, one
        # queued, one in hand): the render gets no further while the
        # upload stalls, so a stalled network never holds a shard of
        # PNGs in memory. The pool and the backlog are read at call
        # time, so the tick helper's patch of both is what this reads.
        download = self._six_page_shard()
        rendered = []
        three_out = threading.Event()
        real = mistral_ocr.render_shard_pages

        def spy(pdf_path, whiteout=False):
            for page in real(pdf_path, whiteout=whiteout):
                rendered.append(page[0])
                if len(rendered) == 3:
                    three_out.set()
                yield page

        ahead = []

        def upload(name, content, purpose):
            if name.endswith("manifest.jsonl"):
                return "m"
            if name.endswith("p0.png"):
                three_out.wait(timeout=10)
                # The feed is parked in ``_take_slot`` now; a moment
                # more proves it renders nothing further.
                time.sleep(0.2)
                ahead.append(len(rendered))
            return name

        with patch("scanning.mistral_ocr.render_shard_pages", spy):
            summary, _ = self._tick(
                download=download, upload_error=upload, workers=1
            )

        self.assertEqual(summary.submitted, 1)
        self.assertEqual(ahead, [3])
        self.assertEqual(len(rendered), 6)

    def test_a_held_submit_lock_claims_nothing(self):
        # Another worker's wave holds the lock: this one claims no row.
        other = connections.create_connection("default")
        self.addCleanup(other.close)
        key = [locks.NAMESPACE, locks.key(mistral_ocr.SUBMIT_LOCK)]
        with other.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s, %s)", key)
        try:
            summary, mocks = self._tick()
        finally:
            with other.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s, %s)", key)
        self.assertEqual(summary.submitted, 0)
        mocks["upload"].assert_not_called()
        self.assertEqual(extract_jobs(self.scan)[0].status, JobStatus.PENDING)

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
        # The first page uploaded, the second was declined on every
        # try: nothing is wrong with the job, so the attempt is intact
        # and the orphan file is deleted.
        busy = mistral_client.MistralBusy("slow down", "RATE_LIMITED", 429)
        summary, mocks = self._tick(
            upload_error=["f0"] + [busy] * (mistral_ocr.UPLOAD_RETRIES + 1)
        )
        self.assertEqual(summary.deferred, 1)
        job = extract_jobs(self.scan)[0]
        self.assertEqual(job.status, JobStatus.PENDING)
        self.assertEqual(job.attempt, 1)
        self.assertEqual(job.error_code, "RATE_LIMITED")
        mocks["delete"].assert_called_once_with("f0")
        mocks["create"].assert_not_called()
        self.assertEqual(
            mocks["upload"].call_count, 2 + mistral_ocr.UPLOAD_RETRIES
        )

    def test_a_throttled_page_is_tried_again_and_the_shard_goes_on(self):
        # The second page is declined twice and then taken: the page
        # waited on its own thread, the shard was not thrown away, and
        # the batch names both pages.
        busy = mistral_client.MistralBusy("slow down", "RATE_LIMITED", 429)
        summary, mocks = self._tick(
            upload_error=["f0", busy, busy, "f1", "m1"]
        )
        self.assertEqual(summary.submitted, 1)
        self.assertEqual(mocks["upload"].call_count, 5)
        mocks["delete"].assert_not_called()
        job = extract_jobs(self.scan)[0]
        self.assertEqual(job.external_id, "batch-1")
        self.assertEqual(job.provider_meta["files"], ["f0", "f1", "m1"])
        manifest = mocks["upload"].call_args_list[-1].args[1].decode()
        self.assertEqual(
            [
                json.loads(line)["body"]["document"]["file_id"]
                for line in manifest.splitlines()
            ],
            ["f0", "f1"],
        )

    def test_the_backoff_doubles_between_tries(self):
        # Each wait is the base doubled per try, plus a jitter under one
        # base, and the stop flag is read as it goes.
        busy = mistral_client.MistralBusy("slow down", "RATE_LIMITED", 429)
        waits = []
        with (
            patch.object(mistral_ocr, "UPLOAD_RETRIES", 3),
            patch.object(mistral_ocr, "UPLOAD_BACKOFF_SECONDS", 1.0),
            patch.object(
                mistral_ocr,
                "_wait_out",
                side_effect=lambda seconds, abandoned: waits.append(seconds),
            ),
            patch(
                "scanning.mistral_client.upload_file",
                side_effect=[busy, busy, busy, "f0"],
            ),
        ):
            self.assertEqual(
                mistral_ocr._upload_page(
                    "x-", 0, b"", threading.Event(), threading.Semaphore(1)
                ),
                "f0",
            )
        self.assertEqual(len(waits), 3)
        for tried, wait in enumerate(waits):
            self.assertGreaterEqual(wait, 2**tried)
            self.assertLess(wait, 2**tried + 1)

    def test_a_stop_during_a_backoff_hands_the_row_back_at_once(self):
        # The first page is throttled and the worker is told to stop
        # while it waits: the wait ends at once, nothing more uploads,
        # and the row goes back with its attempt intact.
        self.addCleanup(stopping.reset)

        def upload(name, content, purpose):
            stopping.request()
            raise mistral_client.MistralBusy("slow down", "RATE_LIMITED", 429)

        started = time.monotonic()
        summary, mocks = self._tick(upload_error=upload, backoff=30.0)

        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(summary.deferred, 1)
        self.assertEqual(mocks["upload"].call_count, 1)
        mocks["create"].assert_not_called()
        mocks["delete"].assert_not_called()
        job = extract_jobs(self.scan)[0]
        self.assertEqual(job.status, JobStatus.PENDING)
        self.assertEqual(job.attempt, 1)
        self.assertEqual(job.error_code, mistral_ocr.STOPPED_ERROR_CODE)

    def test_a_refused_page_ends_a_throttled_one_s_tries_at_once(self):
        # Three upload threads over six pages: page 1 is throttled on
        # every try and would wait out a long backoff, page 2 is
        # refused for good. The shard is abandoned, the throttled page
        # lets go of its wait at once, the row fails, and the wave does
        # not sit behind the tries.
        download = self._six_page_shard()
        busy = mistral_client.MistralBusy("slow down", "RATE_LIMITED", 429)
        landed = []
        lock = threading.Lock()

        def upload(name, content, purpose):
            page = _page_of(name)
            if page == 1:
                raise busy
            if page == 2:
                time.sleep(0.05)
                raise mistral_client.MistralError("bad", "HTTP_400", 400)
            with lock:
                landed.append(f"id-p{page}")
            return f"id-p{page}"

        started = time.monotonic()
        summary, mocks = self._tick(
            download=download, upload_error=upload, workers=3, backoff=30.0
        )

        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(summary.failed, 1)
        self.assertEqual(extract_jobs(self.scan)[0].status, JobStatus.FAILED)
        mocks["create"].assert_not_called()
        self.assertEqual(
            sorted(c.args[0] for c in mocks["delete"].call_args_list),
            sorted(landed),
        )
        # The throttled page was tried once and let go, not retried.
        tries = [
            c
            for c in mocks["upload"].call_args_list
            if _page_of(c.args[0]) == 1
        ]
        self.assertEqual(len(tries), 1)

    def test_a_page_queued_behind_a_stop_never_uploads(self):
        # One upload thread and a backlog of two, so a page is queued
        # behind the one uploading; the upload of the first page asks
        # for the stop, and the queued page starts and stops at once
        # rather than uploading after the stop.
        self.addCleanup(stopping.reset)
        download = self._six_page_shard()
        uploaded = []

        def upload(name, content, purpose):
            uploaded.append(name)
            time.sleep(0.05)
            stopping.request()
            return name

        summary, mocks = self._tick(
            download=download, upload_error=upload, workers=1
        )

        self.assertEqual(summary.deferred, 1)
        self.assertEqual(uploaded, [uploaded[0]])
        mocks["create"].assert_not_called()
        self.assertEqual(
            [c.args[0] for c in mocks["delete"].call_args_list], uploaded
        )

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

    def test_the_per_tick_cap_follows_the_mode(self):
        # One shard per tick under the daemon, whose serial scheduler
        # waits behind the wave; a volume's worth under Celery, where
        # the wave is its own locked task; the setting overrides both.
        with override_settings(
            CELERY_ENABLED=False, MISTRAL_SUBMITS_PER_TICK=0
        ):
            self.assertEqual(mistral_ocr.submits_per_tick(), 1)
        with override_settings(
            CELERY_ENABLED=True, MISTRAL_SUBMITS_PER_TICK=0
        ):
            self.assertEqual(
                mistral_ocr.submits_per_tick(), mistral_ocr.VOLUME_SHARDS
            )
        with override_settings(
            CELERY_ENABLED=False, MISTRAL_SUBMITS_PER_TICK=3
        ):
            self.assertEqual(mistral_ocr.submits_per_tick(), 3)

    def test_a_volume_s_shards_go_in_one_wave(self):
        # With the cap at a volume's worth, one tick renders, uploads
        # and submits every shard of the volume, each as its own batch.
        scan = ScanFactory()
        mistral_ocr.ensure_extract_jobs(
            scan, make_manifest(shard_count=3, pages_per_shard=2)
        )
        summary, mocks = self._tick(
            upload_error=lambda name, content, purpose: name,
            workers=2,
            per_tick=10,
        )
        # The three, plus the fixture's own shard.
        self.assertEqual(summary.submitted, 4)
        self.assertEqual(mocks["create"].call_count, 4)
        self.assertEqual(
            [
                job.status
                for job in ExternalJob.objects.filter(scan=scan).order_by(
                    "shard_index"
                )
            ],
            [JobStatus.SUBMITTED] * 3,
        )

    def test_the_backlog_is_the_wave_s_and_not_the_shard_s(self):
        # Two shards in one wave, one upload thread and a backlog of
        # two, the first upload held: the wave has two pages out (one
        # uploading, one waiting on the slot) and each shard one in
        # hand, four in all, and neither shard renders further until
        # the upload lets go. A backlog per shard would have six.
        ExternalJob.objects.filter(scan=self.scan).delete()
        pdf = self.tmp / "six.pdf"
        write_pdf(pdf, pages=6)
        mistral_ocr.ensure_extract_jobs(
            self.scan, make_manifest(shard_count=2, pages_per_shard=6)
        )
        rendered = []
        lock = threading.Lock()
        four_out = threading.Event()
        real = mistral_ocr.render_shard_pages

        def spy(pdf_path, whiteout=False):
            for page in real(pdf_path, whiteout=whiteout):
                with lock:
                    rendered.append(page[0])
                    if len(rendered) == 4:
                        four_out.set()
                yield page

        ahead = []
        held = [False]

        def upload(name, content, purpose):
            if name.endswith("manifest.jsonl"):
                return name
            with lock:
                first, held[0] = not held[0], True
            if first:
                four_out.wait(timeout=10)
                time.sleep(0.3)
                with lock:
                    ahead.append(len(rendered))
            return name

        with patch("scanning.mistral_ocr.render_shard_pages", spy):
            summary, _ = self._tick(
                download=lambda key, dest: shutil.copy(pdf, dest),
                upload_error=upload,
                workers=1,
                per_tick=10,
            )

        self.assertEqual(summary.submitted, 2)
        self.assertEqual(ahead, [4])
        self.assertEqual(len(rendered), 12)

    def _two_shards(self):
        """Replace the fixture's shard with two six-page shards of one
        volume, and return the rows in shard order and the download
        stub."""
        ExternalJob.objects.filter(scan=self.scan).delete()
        pdf = self.tmp / "six.pdf"
        write_pdf(pdf, pages=6)
        mistral_ocr.ensure_extract_jobs(
            self.scan, make_manifest(shard_count=2, pages_per_shard=6)
        )
        rows = list(extract_jobs(self.scan))
        return rows, (lambda key, dest: shutil.copy(pdf, dest))

    def test_a_batch_id_is_written_the_moment_its_shard_is_done(self):
        # Two shards in one wave, the second held on its first upload:
        # the first shard's outcome is written while the second still
        # uploads, so a worker lost mid-wave loses no batch it paid
        # for. The held upload sees the first row's outcome applied.
        rows, download = self._two_shards()
        first_applied = threading.Event()
        seen = []
        real = mistral_ocr._apply_outcome

        def spy(job, exc, submission, now, **kwargs):
            result = real(job, exc, submission, now, **kwargs)
            if job.pk == rows[0].pk:
                first_applied.set()
            return result

        def upload(name, content, purpose):
            if f"-job{rows[1].pk}-" in name and name.endswith("p0.png"):
                seen.append(first_applied.wait(timeout=5))
            return name

        with patch("scanning.mistral_ocr._apply_outcome", spy):
            summary, _ = self._tick(
                download=download, upload_error=upload, workers=2, per_tick=10
            )

        self.assertEqual(summary.submitted, 2)
        self.assertEqual(seen, [True])

    def test_the_renders_are_capped_apart_from_the_shards_per_wave(self):
        # Three shards in one wave and one render worker: the shard
        # PDFs come down one at a time, so a wave of a whole volume
        # never holds every shard on disk at once.
        scan = ScanFactory()
        mistral_ocr.ensure_extract_jobs(
            scan, make_manifest(shard_count=3, pages_per_shard=2)
        )
        lock = threading.Lock()
        running = [0]
        most = [0]

        def download(key, dest):
            with lock:
                running[0] += 1
                most[0] = max(most[0], running[0])
            time.sleep(0.05)
            shutil.copy(self.pdf, dest)
            with lock:
                running[0] -= 1

        summary, _ = self._tick(
            download=download,
            upload_error=lambda name, content, purpose: name,
            workers=4,
            per_tick=10,
            renders=1,
        )
        self.assertEqual(summary.submitted, 4)
        self.assertEqual(most[0], 1)

    def test_a_page_refused_after_the_last_is_handed_over_ends_the_shard(
        self,
    ):
        # Three upload threads and a backlog of six, so every page of
        # the six-page shard is handed over before any answers; then
        # the last page is refused for good while another is throttled
        # and would wait out a long backoff. The shard is abandoned all
        # the same: the throttled page lets go at once and is not tried
        # again, and the row fails within seconds.
        download = self._six_page_shard()
        busy = mistral_client.MistralBusy("slow down", "RATE_LIMITED", 429)

        def upload(name, content, purpose):
            page = _page_of(name)
            if page == 4:
                time.sleep(0.05)
                raise busy
            if page == 5:
                time.sleep(0.1)
                raise mistral_client.MistralError("bad", "HTTP_400", 400)
            return name

        started = time.monotonic()
        summary, mocks = self._tick(
            download=download, upload_error=upload, workers=3, backoff=30.0
        )

        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(summary.failed, 1)
        self.assertEqual(extract_jobs(self.scan)[0].status, JobStatus.FAILED)
        tries = [
            c
            for c in mocks["upload"].call_args_list
            if _page_of(c.args[0]) == 4
        ]
        self.assertEqual(len(tries), 1)

    def test_a_file_no_delete_reached_is_carried_and_deleted_next_time(self):
        # The first page landed, the second failed for now, and the
        # delete of the first did not land: the row carries the id, the
        # retry's cancel tries it again, and the next submission deletes
        # it first and drops it from the row once the batch is submitted.
        transient = mistral_client.MistralTransientError(
            "down", "HTTP_503", 503
        )
        summary, mocks = self._tick(
            upload_error=["f0", transient], delete_ok=False
        )
        self.assertEqual(summary.retried, 1)
        job = extract_jobs(self.scan)[0]
        self.assertEqual(job.status, JobStatus.PENDING)
        self.assertEqual(job.provider_meta["files_left"], ["f0"])

        summary, mocks = self._tick(
            upload=["g0", "g1", "m2"], create="batch-2"
        )
        self.assertEqual(summary.submitted, 1)
        self.assertEqual(mocks["delete"].call_args_list[0].args, ("f0",))
        job = extract_jobs(self.scan)[0]
        self.assertEqual(job.external_id, "batch-2")
        self.assertNotIn("files_left", job.provider_meta)
        self.assertEqual(job.provider_meta["files"], ["g0", "g1", "m2"])

    def test_a_delete_is_one_call_and_what_it_misses_is_carried(self):
        # No delete waits: a throttled id and a refused one are handed
        # back in the caller's order, each after one call, for the row
        # to carry and the sweep to try again.
        busy = mistral_client.MistralBusy("slow down", "RATE_LIMITED", 429)
        with patch(
            "scanning.mistral_client.delete_file",
            side_effect=lambda file_id: _answer(
                {"f0": [busy], "f1": [True], "f2": [False]}[file_id]
            ),
        ) as delete:
            self.assertEqual(
                mistral_ocr._delete_files(["f0", "f1", "", "f2"]),
                ["f0", "f2"],
            )
        self.assertEqual(delete.call_count, 3)

    def test_a_stop_keeps_a_queued_shard_from_pulling_its_pdf(self):
        # Two shards and one render worker: the first shard's upload
        # asks for the stop, and the second, queued behind the cap,
        # reads it before it downloads anything.
        self.addCleanup(stopping.reset)
        rows, download = self._two_shards()

        def upload(name, content, purpose):
            stopping.request()
            return name

        summary, mocks = self._tick(
            download=download, upload_error=upload, per_tick=2, renders=1
        )
        self.assertEqual(summary.deferred, 2)
        self.assertEqual(mocks["download"].call_count, 1)
        mocks["create"].assert_not_called()

    def test_an_outcome_write_that_raises_cancels_the_batch(self):
        # The database fault that stops the write would leave the batch
        # to the lost-claim sweep and pay for the shard twice: the wave
        # cancels it, deletes its files and goes on.
        with (
            patch.object(
                mistral_ocr, "_apply_outcome", side_effect=RuntimeError("db")
            ),
            self.assertLogs("scanning.mistral_ocr", "ERROR"),
        ):
            summary, mocks = self._tick()
        self.assertEqual(summary.unanswered, 1)
        self.assertEqual(summary.submitted, 0)
        mocks["cancel"].assert_called_once_with("batch-1")
        self.assertEqual(
            [c.args[0] for c in mocks["delete"].call_args_list],
            ["f0", "f1", "m1"],
        )
        job = extract_jobs(self.scan)[0]
        self.assertEqual(job.status, JobStatus.SUBMITTED)
        self.assertEqual(job.external_id, "")

    def test_a_write_off_deletes_the_files_the_row_carries(self):
        # A row failed with no batch id still reaches the cancel, which
        # deletes the files an earlier delete could not, and drops the
        # key once they are gone.
        job = extract_jobs(self.scan)[0]
        ExternalJob.objects.filter(pk=job.pk).update(
            provider_meta={"files": ["e0"], "files_left": ["f0", "f1"]}
        )
        job.refresh_from_db()
        with (
            patch("scanning.mistral_client.delete_file") as delete,
            patch("scanning.mistral_client.cancel_batch") as cancel,
        ):
            jobs._fail(job, "REFUSED", "no")
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertEqual(
            [c.args[0] for c in delete.call_args_list], ["e0", "f0", "f1"]
        )
        cancel.assert_not_called()
        # Both keys go: the next write-off of a retried row would
        # delete the files again otherwise.
        self.assertNotIn("files_left", job.provider_meta)
        self.assertNotIn("files", job.provider_meta)

    def test_two_carriers_keep_each_others_files(self):
        # Each carry takes off only what its caller tried and adds what
        # it could not reach, so the wave's thread and an abandon that
        # carry at once drop neither's ids.
        job = extract_jobs(self.scan)[0]
        ExternalJob.objects.filter(pk=job.pk).update(
            provider_meta={"files": ["e0"], "files_left": ["a", "b"]}
        )
        job.refresh_from_db()
        mistral_ocr._carry_files_left(job, ["a"], [])
        self.assertEqual(job.provider_meta["files_left"], ["b"])
        with self.assertLogs("scanning.mistral_ocr", "WARNING"):
            mistral_ocr._carry_files_left(job, ["c", "d"], ["c"])
        self.assertEqual(job.provider_meta["files_left"], ["b", "c"])
        self.assertEqual(job.provider_meta["files"], ["e0"])
        mistral_ocr._carry_files_left(
            job, ["e0", "b", "c"], [], drop_files=True
        )
        job.refresh_from_db()
        self.assertNotIn("files_left", job.provider_meta)
        self.assertNotIn("files", job.provider_meta)

    def test_the_admin_delete_tries_the_carried_files_once_more(self):
        # The rows go with the scan, and their files_left with them:
        # one last delete, and the ids it misses are logged.
        busy = mistral_client.MistralBusy("slow down", "RATE_LIMITED", 429)
        job = extract_jobs(self.scan)[0]
        ExternalJob.objects.filter(pk=job.pk).update(
            status=JobStatus.CANCELLED,
            provider_meta={"files_left": ["f0", "f1"]},
        )
        with (
            patch(
                "scanning.mistral_client.delete_file",
                side_effect=lambda file_id: _answer(
                    {"f1": [busy]}.get(file_id, [True])
                ),
            ) as delete,
            self.assertLogs("scanning.mistral_ocr", "WARNING") as logs,
        ):
            self.assertEqual(mistral_ocr.release_scan_files(self.scan), ["f1"])
        self.assertEqual(
            [c.args[0] for c in delete.call_args_list], ["f0", "f1"]
        )
        self.assertIn("f1", logs.output[0])

    def test_the_in_flight_cap_holds_a_tick_back(self):
        scan = ScanFactory()
        mistral_ocr.ensure_extract_jobs(
            scan, make_manifest(shard_count=3, pages_per_shard=2)
        )
        summary, _ = self._tick(
            upload=["f"] * 12, create="batch-1", per_tick=10, cap=2
        )
        self.assertEqual(summary.submitted, 2)
        summary, _ = self._tick(
            upload=["f"] * 12, create="batch-2", per_tick=10, cap=2
        )
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
                            "blocks": [
                                {
                                    "type": "text",
                                    "top_left_x": 100,
                                    "top_left_y": 200,
                                    "bottom_right_x": 900,
                                    "bottom_right_y": 300,
                                    "content": "One",
                                }
                            ],
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

    def _sweep(
        self,
        outcome,
        now=None,
        output=None,
        errors=None,
        put=True,
        delete=None,
    ):
        def download(file_id):
            return {"out-1": output or [], "err-1": errors or []}[file_id]

        patches = [
            patch("scanning.mistral_client.poll_batch", return_value=outcome),
            patch(
                "scanning.mistral_client.download_lines", side_effect=download
            ),
            patch("scanning.s3_sync.upload_json_object", return_value=put),
            patch("scanning.mistral_client.delete_file", side_effect=delete),
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

    def test_a_harvest_carries_the_files_it_could_not_delete(self):
        # The result is in S3 and the row complete; a file a throttled
        # delete did not reach stays named on the row for the sweep's
        # own pass, held off here, instead of being dropped with the
        # key.
        busy = mistral_client.MistralBusy("slow down", "RATE_LIMITED", 429)
        outcome = mistral_client.BatchOutcome(
            status=JobStatus.COMPLETED,
            provider_status="SUCCESS",
            output_file="out-1",
            job={},
        )
        with (
            patch.object(mistral_ocr, "LEFTOVER_ROWS_PER_TICK", 0),
            self.assertLogs("scanning.mistral_ocr", "WARNING") as logs,
        ):
            summary, mocks = self._sweep(
                outcome,
                output=self.OUTPUT,
                delete=lambda file_id: _answer(
                    {"f1": [busy]}.get(file_id, [True])
                ),
            )
        self.assertEqual(summary.completed, 1)
        self.assertEqual(self.job.status, JobStatus.COMPLETED)
        self.assertNotIn("files", self.job.provider_meta)
        self.assertEqual(self.job.provider_meta["files_left"], ["f1"])
        self.assertTrue(any("carries them" in line for line in logs.output))

    def test_the_sweep_deletes_what_a_finished_row_carries(self):
        # A COMPLETED row no later write reaches still names a file: the
        # sweep tries it every tick and drops the key once it is gone.
        # A row in flight is left to its harvest.
        busy = mistral_client.MistralBusy("slow down", "RATE_LIMITED", 429)
        ExternalJob.objects.filter(pk=self.job.pk).update(
            status=JobStatus.COMPLETED,
            provider_meta={"files_left": ["f1", "f2"], "output": {}},
        )
        other = ScanFactory()
        (in_flight,) = mistral_ocr.ensure_extract_jobs(
            other, make_manifest(shard_count=1, pages_per_shard=2)
        )
        ExternalJob.objects.filter(pk=in_flight.pk).update(
            status=JobStatus.IN_PROGRESS,
            external_id="batch-2",
            provider_meta={"files_left": ["g0"]},
        )
        answers = {"f1": [busy, True], "f2": [True], "g0": [True]}
        with (
            patch(
                "scanning.mistral_client.delete_file",
                side_effect=lambda file_id: _answer(answers[file_id]),
            ) as delete,
            patch(
                "scanning.mistral_client.poll_batch",
                return_value=mistral_client.BatchOutcome(status=None),
            ),
            self.assertLogs("scanning.mistral_ocr", "WARNING"),
        ):
            jobs.sweep_jobs()
            self.job.refresh_from_db()
            self.assertEqual(self.job.provider_meta["files_left"], ["f1"])
            self.assertEqual(self.job.status, JobStatus.COMPLETED)
            jobs.sweep_jobs()
        self.job.refresh_from_db()
        self.assertNotIn("files_left", self.job.provider_meta)
        self.assertEqual(self.job.provider_meta["output"], {})
        self.assertEqual(
            [c.args[0] for c in delete.call_args_list], ["f1", "f2", "f1"]
        )
        in_flight.refresh_from_db()
        self.assertEqual(in_flight.provider_meta["files_left"], ["g0"])

    def test_the_sweep_survives_a_row_deleted_under_it(self):
        # The row goes between the pass's select and its stamp: the
        # stamp fails, the tick logs it and ends as it should.
        ExternalJob.objects.filter(pk=self.job.pk).update(
            status=JobStatus.COMPLETED, provider_meta={"files_left": ["f1"]}
        )

        def delete(file_id):
            ExternalJob.objects.filter(pk=self.job.pk).delete()
            return True

        with (
            patch("scanning.mistral_client.delete_file", side_effect=delete),
            self.assertLogs("scanning.mistral_ocr", "WARNING") as logs,
        ):
            jobs.sweep_jobs()
        self.assertIn("may be gone", logs.output[-1])

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

    def test_a_missing_output_file_retries_the_shard(self):
        # The cascade returns inside its completion branch, before the
        # deadline check, so a 404 answered "next tick" would be a row
        # nothing ever ends. It costs an attempt instead.
        outcome = mistral_client.BatchOutcome(
            status=JobStatus.COMPLETED,
            provider_status="SUCCESS",
            output_file="out-1",
            job={},
        )
        with (
            patch(
                "scanning.mistral_client.download_lines",
                side_effect=mistral_client.MistralMissing(
                    "gone", "NOT_FOUND", 404
                ),
            ),
            patch("scanning.mistral_client.poll_batch", return_value=outcome),
            patch("scanning.mistral_client.cancel_batch"),
            patch("scanning.mistral_client.delete_file") as delete,
            patch("scanning.s3_sync.upload_json_object") as put,
            self.assertLogs("scanning.mistral_ocr", "WARNING"),
        ):
            summary = jobs.sweep_jobs()
        self.job.refresh_from_db()
        self.assertEqual(summary.retried, 1)
        self.assertEqual(self.job.status, JobStatus.PENDING)
        self.assertEqual(self.job.attempt, 2)
        self.assertEqual(self.job.external_id, "")
        self.assertEqual(self.job.error_code, "NOT_FOUND")
        put.assert_not_called()
        # The retry deletes what the dead attempt uploaded.
        self.assertEqual(
            [call.args[0] for call in delete.call_args_list],
            ["f0", "f1", "m1"],
        )

    def test_a_put_that_keeps_failing_ends_at_the_deadline(self):
        # A finished batch whose result never lands holds the row the
        # same way a missing file would. The harvest judges the
        # deadline itself, because the cascade never reaches it.
        now = timezone.now()
        ExternalJob.objects.filter(pk=self.job.pk).update(
            deadline=now - timedelta(minutes=1)
        )
        summary, _ = self._sweep(
            mistral_client.BatchOutcome(
                status=JobStatus.COMPLETED,
                provider_status="SUCCESS",
                output_file="out-1",
                job={},
            ),
            now=now,
            put=False,
        )
        self.assertEqual(summary.retried, 1)
        self.assertEqual(self.job.status, JobStatus.PENDING)
        self.assertEqual(self.job.attempt, 2)
        self.assertEqual(self.job.error_code, "DEADLINE_EXCEEDED")

    def test_a_row_polled_inside_the_interval_is_left_alone(self):
        # A batch waits for hours, so the sweep asks about it rarely.
        now = timezone.now()
        ExternalJob.objects.filter(pk=self.job.pk).update(last_polled_at=now)
        summary, mocks = self._sweep(
            mistral_client.BatchOutcome(
                status=JobStatus.IN_PROGRESS, provider_status="RUNNING"
            ),
            now=now + mistral_ocr.POLL_INTERVAL - timedelta(seconds=1),
        )
        mocks["poll"].assert_not_called()
        self.assertEqual(summary.pending, 1)
        self.assertEqual(self.job.status, JobStatus.SUBMITTED)

    def test_a_skipped_poll_still_judges_the_deadline(self):
        # The interval delays a question to Mistral, never a write-off.
        now = timezone.now()
        ExternalJob.objects.filter(pk=self.job.pk).update(
            last_polled_at=now, deadline=now - timedelta(minutes=1)
        )
        summary, mocks = self._sweep(
            mistral_client.BatchOutcome(
                status=JobStatus.IN_PROGRESS, provider_status="RUNNING"
            ),
            now=now,
        )
        mocks["poll"].assert_not_called()
        self.assertEqual(summary.pending, 0)
        self.assertEqual(summary.retried, 1)
        self.assertEqual(self.job.status, JobStatus.PENDING)

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

    def _lose_the_id(self):
        ExternalJob.objects.filter(pk=self.job.pk).update(
            external_id="", provider_meta={}
        )
        self.job.refresh_from_db()

    def _sweep_lost(self, found=(), **kwargs):
        finder = patch(
            "scanning.mistral_client.find_files", return_value=list(found)
        )
        find = finder.start()
        self.addCleanup(finder.stop)
        summary, mocks = self._sweep(
            mistral_client.BatchOutcome(status=None), **kwargs
        )
        mocks["find"] = find
        return summary, mocks

    def test_a_claim_under_a_held_submit_lock_is_still_rendering(self):
        # A wave holds the lock (#534): the newest claim with no id is
        # the shard it renders now, however long that takes.
        self._lose_the_id()
        with locks.held(mistral_ocr.SUBMIT_LOCK):
            summary, mocks = self._sweep_lost(
                now=timezone.now() + timedelta(hours=2)
            )
        self.assertEqual(summary.pending, 1)
        self.assertEqual(summary.retried, 0)
        self.assertEqual(self.job.status, JobStatus.SUBMITTED)
        mocks["poll"].assert_not_called()
        mocks["find"].assert_not_called()

    def test_a_claim_with_no_lock_held_is_lost_and_its_files_deleted(self):
        # No session holds the lock: the worker died with the claim.
        # The files it uploaded are found by their names and deleted
        # before the retry.
        self._lose_the_id()
        summary, mocks = self._sweep_lost(
            found=["f0", "f1"],
            now=self.job.submitted_at + mistral_ocr.LEGACY_CLAIM_GRACE,
        )
        self.assertEqual(summary.retried, 1)
        self.assertEqual(self.job.status, JobStatus.PENDING)
        self.assertEqual(self.job.attempt, 2)
        self.assertEqual(self.job.error_code, "LOST_CLAIM")
        mocks["poll"].assert_not_called()
        mocks["find"].assert_called_once_with(
            f"scan{self.scan.pk}-job{self.job.pk}-a1-"
        )
        deleted = [c.args[0] for c in mocks["delete"].call_args_list]
        self.assertEqual(deleted, ["f0", "f1"])

    def test_a_lost_claim_carries_the_files_it_could_not_delete(self):
        # The files found by name and the ones the row carried from
        # before are deleted together. What a throttle refused is tried
        # once more by the retry's cancel and then carried into the next
        # attempt, whose submission deletes it first (the sweep's own
        # pass is held off here).
        busy = mistral_client.MistralBusy("slow down", "RATE_LIMITED", 429)
        self._lose_the_id()
        ExternalJob.objects.filter(pk=self.job.pk).update(
            provider_meta={"files_left": ["old"]}
        )
        self.job.refresh_from_db()
        with patch.object(mistral_ocr, "LEFTOVER_ROWS_PER_TICK", 0):
            summary, mocks = self._sweep_lost(
                found=["f0", "f1"],
                now=self.job.submitted_at + mistral_ocr.LEGACY_CLAIM_GRACE,
                delete=lambda file_id: _answer(
                    {"f1": [busy]}.get(file_id, [True])
                ),
            )
        self.assertEqual(summary.retried, 1)
        self.assertEqual(self.job.status, JobStatus.PENDING)
        self.assertEqual(self.job.attempt, 2)
        deleted = [c.args[0] for c in mocks["delete"].call_args_list]
        self.assertEqual(deleted, ["f0", "f1", "old", "f1"])
        self.assertEqual(self.job.provider_meta["files_left"], ["f1"])

    def test_a_young_claim_with_no_lock_held_waits_for_one_release(self):
        # A pod on the code before the lock submits without it, so
        # during the rollout a young claim with no lock is alive.
        self._lose_the_id()
        summary, mocks = self._sweep_lost(
            now=self.job.submitted_at
            + mistral_ocr.LEGACY_CLAIM_GRACE
            - timedelta(seconds=1)
        )
        self.assertEqual(summary.pending, 1)
        self.assertEqual(summary.retried, 0)
        self.assertEqual(self.job.status, JobStatus.SUBMITTED)
        mocks["find"].assert_not_called()

    def test_an_older_claim_under_a_held_lock_is_lost(self):
        # The lock is held by a wave rendering a newer claim: this one
        # was left by a worker that died before it.
        self._lose_the_id()
        newer = ExternalJob.objects.get(pk=self.job.pk)
        newer.pk = None
        newer.run = self.job.run + 1
        newer.submitted_at = self.job.submitted_at + timedelta(minutes=1)
        newer.save()
        with locks.held(mistral_ocr.SUBMIT_LOCK):
            summary, _ = self._sweep_lost()
        self.assertEqual(summary.retried, 1)
        self.assertEqual(summary.pending, 1)
        self.assertEqual(self.job.error_code, "LOST_CLAIM")
        newer.refresh_from_db()
        self.assertEqual(newer.status, JobStatus.SUBMITTED)

    def test_an_id_written_before_the_lock_went_is_not_retried(self):
        # The wave wrote the id and let the lock go after the sweep
        # read the row: the row is read again, and its paid batch kept.
        self._lose_the_id()

        def written_meanwhile(job, now):
            ExternalJob.objects.filter(pk=job.pk).update(external_id="batch-2")
            return False

        with patch.object(
            mistral_ocr, "_claim_alive", side_effect=written_meanwhile
        ):
            summary, mocks = self._sweep_lost()
        self.assertEqual(summary.pending, 1)
        self.assertEqual(summary.retried, 0)
        self.assertEqual(self.job.status, JobStatus.SUBMITTED)
        self.assertEqual(self.job.external_id, "batch-2")
        mocks["find"].assert_not_called()
        mocks["cancel"].assert_not_called()


# ── the start button ────────────────────────────────────────────────
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
        # The button gates on the key and the shard set, not on a
        # status: the read is over the original shards, so what it
        # needs exists from the moment the pipeline cut them, and
        # where the stage is called from is a separate question.
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

    def test_a_failed_part_of_an_open_run_says_the_run_goes_on(self):
        # One part failed while the other is still running (#534): the
        # line says both, because the button stays hidden until the run
        # ends, and "failed" alone read as a run with no way back.
        with self._committed():
            self._press()
        failed, running = extract_jobs(self.scan)
        ExternalJob.objects.filter(pk=failed.pk).update(
            status=JobStatus.FAILED, error_code="LOST_CLAIM"
        )
        self.client.force_login(self.staff)
        html = self.client.get(
            reverse("process_actions", kwargs={"pk": self.scan.pk})
        ).json()["html"]
        self.assertIn(
            "Mistral OCR: 1 of 2 part(s) failed (LOST_CLAIM), 1 still "
            "running; re-run once they end",
            " ".join(html.split()),
        )
        self.assertNotIn("Re-run Mistral OCR", html)

        ExternalJob.objects.filter(pk=running.pk).update(
            status=JobStatus.CONSUMED
        )
        html = self.client.get(
            reverse("process_actions", kwargs={"pk": self.scan.pk})
        ).json()["html"]
        self.assertIn("Mistral OCR failed on 1 of 2 part(s)", html)
        self.assertIn("Re-run Mistral OCR", html)

    def test_the_bar_offers_the_button_to_staff(self):
        # Beside the dots.mocr control on the step-1 bar, because the
        # read is over the same shards that control reads.
        url = reverse("process_actions", kwargs={"pk": self.scan.pk})
        self.client.force_login(self.staff)
        self.assertIn(
            "Run Mistral OCR",
            self.client.get(f"{url}?step=1").json()["html"],
        )

    def test_the_bar_offers_a_curator_no_button(self):
        # The template gate, ahead of the view's: a curator is never
        # offered work that costs money.
        url = reverse("process_actions", kwargs={"pk": self.scan.pk})
        self.client.force_login(self.make_user())
        self.assertNotIn(
            "Run Mistral OCR",
            self.client.get(f"{url}?step=1").json()["html"],
        )
