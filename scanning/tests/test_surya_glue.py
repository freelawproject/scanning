"""Tests for the Surya volume glue (issue #368).

S3 is inert under ``TESTING``, so the glue's downloads are patched to
copy envelopes from a local directory and its upload is captured, as
the dots.mocr glue's tests do. Under test:

- the one transform: the pages keyed by their page inside the shard,
  and ``raw`` dropped
- the page arithmetic: shard-local ``page_no`` to volume ``page_index``
- the four page lists, and their agreement with the lists the worker
  wrote
- the finish pass: rows consumed, results kept, no scan status
  written, and a bounded retry of a glue that keeps failing
"""

import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from scanning import dots_mocr, surya
from scanning.factories import ScanFactory
from scanning.models import ExternalJob, JobStatus, Scan, Status
from scanning.tests.test_jobs import make_manifest


def make_block(text: str = "878 N. C.") -> dict:
    """Build one block of the shape the worker serializes.

    :param text: The block's text.
    :returns: A block dict.
    :rtype: dict
    """
    return {
        "order": 0,
        "label": "SectionHeader",
        "raw_label": "section-header",
        "bbox": [276, 93, 426, 129],
        "confidence": 0.98,
        "html": f"<h2>{text}</h2>",
        "text": text,
        "skipped": False,
        "error": False,
    }


def make_page(page_no: int, text: str = "878 N. C.") -> dict:
    """Build one page dict of the shape the worker reports.

    :param page_no: 0-based page inside the shard.
    :param text: The text of the page's one block.
    :returns: A page dict.
    :rtype: dict
    """
    return {
        "page_no": page_no,
        "origin_width": 1700,
        "origin_height": 2200,
        "blocks": [make_block(text)],
        "text": text,
        # The answer of the whole page. The glue drops it.
        "raw": f"<div>{text}</div>",
        "requests": 1,
        "completion_tokens": 42,
        "attempts": 1,
        "duration_ms": 900,
        "confidence": 0.97,
        "raw_divs": 1,
        "parsed_blocks": 1,
    }


def make_failed_page(page_no: int) -> dict:
    """Build a page whose read raised: no block, and no render size.

    :param page_no: 0-based page inside the shard.
    :returns: A page dict.
    :rtype: dict
    """
    return {
        "page_no": page_no,
        "error": "read failed: the server closed the socket",
        "attempts": 2,
        "raw": None,
    }


def make_empty_page(page_no: int) -> dict:
    """Build a page the worker read twice and got no block from.

    :param page_no: 0-based page inside the shard.
    :returns: A page dict.
    :rtype: dict
    """
    page = make_page(page_no)
    page.update(
        {
            "blocks": [],
            "text": "",
            "empty": True,
            "attempts": 2,
            "raw_divs": 0,
            "parsed_blocks": 0,
        }
    )
    return page


def make_fallback_page(page_no: int) -> dict:
    """Build a page surya read block by block after the whole read failed.

    :param page_no: 0-based page inside the shard.
    :returns: A page dict.
    :rtype: dict
    """
    page = make_page(page_no)
    page.update({"fallback": "block", "error_blocks": 1})
    return page


def make_dropped_page(page_no: int) -> dict:
    """Build a page whose answer held a div its parser refused.

    :param page_no: 0-based page inside the shard.
    :returns: A page dict.
    :rtype: dict
    """
    page = make_page(page_no)
    page.update(
        {
            "raw_divs": 3,
            "parsed_blocks": 2,
            "refused_divs": [{"div": "<div>x", "raw_label": "text"}],
        }
    )
    return page


def _fixture_lost_content(page: dict) -> bool:
    """Whether one page lost content, written out by hand.

    The third copy of the rule, and the fixture's own: the worker has
    one (``handler._lost_content``) and the glue has one
    (``surya._lost_content``), and a test of this module compares those
    two against each other. This one is here so the lists of a payload
    are not computed by the code under test.

    :param page: One page of a result.
    :returns: Whether a div was refused or a parsed entry has no block.
    :rtype: bool
    """
    if page.get("dropped_blocks"):
        return True
    divs = page.get("raw_divs")
    if not isinstance(divs, int):
        return False
    parsed = page.get("parsed_blocks")
    return divs > (parsed if isinstance(parsed, int) else 0)


def make_payload(pages: list[dict]) -> dict:
    """Build the payload the worker PUTs for one shard.

    The four page lists are computed here the way the worker computes
    them, so a test can compare them with the lists the glue derives.

    :param pages: The shard's page dicts.
    :returns: The payload.
    :rtype: dict
    """
    return {
        "pages": pages,
        "page_count": len(pages),
        "failed_pages": [p["page_no"] for p in pages if "error" in p],
        "empty_pages": [p["page_no"] for p in pages if p.get("empty")],
        "fallback_pages": [p["page_no"] for p in pages if "fallback" in p],
        "dropped_block_pages": [
            p["page_no"] for p in pages if _fixture_lost_content(p)
        ],
        "duration_ms": 9000,
    }


def make_envelope(job: ExternalJob, pages: list[dict], **overrides) -> dict:
    """Build the result envelope one worker attempt PUTs to S3.

    :param job: The row the envelope answers.
    :param pages: The payload's page dicts.
    :param overrides: Envelope fields to replace.
    :returns: An envelope dict.
    :rtype: dict
    """
    envelope = {
        "schema_version": 1,
        "action": surya.ACTION,
        "scan_pk": job.scan_id,
        "result_key": job.result_key,
        "payload": make_payload(pages),
    }
    envelope.update(overrides)
    return envelope


class SuryaRunMixin:
    """Builds a scan whose Surya rows have results on 'S3'."""

    def build(self, shard_count=3, pages_per_shard=2, pages=None):
        """Create a scan, its rows, and an envelope per shard.

        :param shard_count: Shards to create.
        :param pages_per_shard: Pages each shard covers.
        :param pages: ``{shard index: page dicts}`` to store instead of
            the plain pages, for one named shard.
        :returns: ``(scan, rows)``.
        """
        self.store = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.store, True)

        scan = ScanFactory(page_count=shard_count * pages_per_shard)
        rows = surya.ensure_extract_jobs(
            scan, make_manifest(shard_count, pages_per_shard)
        )
        for index, job in enumerate(rows):
            job.status = JobStatus.COMPLETED
            job.result_key = f"jobs/extract/surya/r1-s{index}-a1.json"
            job.save()
            shard_pages = (pages or {}).get(
                index, [make_page(n) for n in range(pages_per_shard)]
            )
            self.write_envelope(index, make_envelope(job, shard_pages))

        def _download(key, dest_path):
            index = int(key.split("-s")[1].split("-")[0])
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(self.store / f"s{index}.json", dest_path)

        download = patch(
            "scanning.s3_sync.download_object", side_effect=_download
        )
        self.download = download.start()
        self.addCleanup(download.stop)
        active = patch("scanning.s3_sync.s3_active", return_value=True)
        active.start()
        self.addCleanup(active.stop)
        self.uploaded: dict[str, dict] = {}

        def _upload(key, document):
            self.uploaded[key] = document
            return True

        upload = patch(
            "scanning.s3_sync.upload_json_object", side_effect=_upload
        )
        self.upload = upload.start()
        self.addCleanup(upload.stop)
        return scan, surya.live_extract_jobs(scan)

    def write_envelope(self, index: int, envelope: dict) -> None:
        """Put ``envelope`` where shard ``index``'s download reads from.

        :param index: The shard index to answer for.
        :param envelope: The envelope to store.
        :return: None.
        """
        (self.store / f"s{index}.json").write_text(json.dumps(envelope))


# ── the transform ───────────────────────────────────────────────────
class TestShardPages(TestCase):
    """The one transform of a stored result."""

    def test_the_pages_come_back_keyed_by_their_page_in_the_shard(self):
        pages = surya.shard_pages(make_payload([make_page(n) for n in (0, 1)]))

        self.assertEqual(sorted(pages), [0, 1])
        self.assertEqual(pages[1]["page_no"], 1)

    def test_raw_is_dropped_and_every_other_field_is_kept(self):
        pages = surya.shard_pages(make_payload([make_page(0)]))

        self.assertNotIn("raw", pages[0])
        self.assertEqual(pages[0]["blocks"], [make_block()])
        self.assertEqual(pages[0]["origin_width"], 1700)
        self.assertEqual(pages[0]["confidence"], 0.97)

    def test_a_page_whose_read_raised_keeps_its_error(self):
        pages = surya.shard_pages(make_payload([make_failed_page(0)]))

        self.assertIn("error", pages[0])
        self.assertNotIn("raw", pages[0])

    def test_a_page_with_no_page_number_is_left_out(self):
        payload = make_payload([make_page(0)])
        payload["pages"].append({"blocks": []})
        payload["pages"].append("not a page")

        self.assertEqual(sorted(surya.shard_pages(payload)), [0])


# ── the page lists ──────────────────────────────────────────────────
class TestPageLists(TestCase):
    """What puts a page in each of the four lists."""

    def test_the_four_lists_name_the_four_faults(self):
        pages = [
            make_page(0),
            make_failed_page(1),
            make_empty_page(2),
            make_fallback_page(3),
            make_dropped_page(4),
        ]

        lists = surya.page_lists(pages, "page_no")

        self.assertEqual(lists["failed_pages"], [1])
        self.assertEqual(lists["empty_pages"], [2])
        self.assertEqual(lists["fallback_pages"], [3])
        self.assertEqual(lists["dropped_block_pages"], [4])

    def test_a_lost_block_is_a_dropped_page_too(self):
        page = make_page(0)
        page["dropped_blocks"] = [{"order": 1}]

        self.assertEqual(
            surya.page_lists([page], "page_no")["dropped_block_pages"], [0]
        )

    def test_a_page_nobody_counted_the_divs_of_lost_nothing(self):
        page = make_page(0)
        del page["raw_divs"]
        del page["parsed_blocks"]

        self.assertEqual(
            surya.page_lists([page], "page_no")["dropped_block_pages"], []
        )

    def test_an_empty_page_is_not_a_failed_page(self):
        lists = surya.page_lists([make_empty_page(0)], "page_no")

        self.assertEqual(lists["failed_pages"], [])
        self.assertEqual(lists["empty_pages"], [0])


class TestTheLostContentRule(TestCase):
    """The glue's copy of the worker's rule, against the worker's own.

    Both answer "did something in the answer not reach the blocks",
    over the same three fields, and the two must agree: the worker
    names the pages of one shard, and the glue names the pages of a
    volume and of a corrected volume, where a page has moved. The
    worker's module is imported here alone, because it needs its
    worker-only dependencies stubbed.
    """

    #: Every shape the two rules are asked about, including the counts
    #: that are absent and the parse that answered nothing.
    SHAPES = (
        {"page_no": 0},
        {"raw_divs": 0, "parsed_blocks": 0},
        {"raw_divs": 2, "parsed_blocks": 2},
        {"raw_divs": 3, "parsed_blocks": 2},
        {"raw_divs": 2, "parsed_blocks": None},
        {"raw_divs": 0, "parsed_blocks": None},
        {"raw_divs": None, "parsed_blocks": 2},
        {"dropped_blocks": [{"order": 1}]},
        {"dropped_blocks": []},
        {"dropped_blocks": [], "raw_divs": 4, "parsed_blocks": 1},
    )

    def test_the_two_copies_answer_the_same_pages(self):
        from scanning.tests.test_runpod_surya_handler import (
            handler as worker,
        )

        for shape in self.SHAPES:
            with self.subTest(shape=shape):
                self.assertIs(
                    surya._lost_content(shape),
                    worker._lost_content(shape),
                )

    def test_the_worker_reports_every_list_the_glue_sorts(self):
        """A list the glue names and the worker's summary drops would
        be empty on every row of the files index."""
        from scanning.tests.test_runpod_surya_handler import (
            handler as worker,
        )

        for name, _member in surya.PAGE_LISTS:
            with self.subTest(name=name):
                self.assertIn(name, worker._SUMMARY_FIELDS)

    def test_the_fixture_answers_the_same_pages_too(self):
        """So a payload's own lists describe what the glue will find."""
        for shape in self.SHAPES:
            with self.subTest(shape=shape):
                self.assertIs(
                    _fixture_lost_content(shape), surya._lost_content(shape)
                )


# ── the volume document ─────────────────────────────────────────────
class TestMergeSuryaResults(SuryaRunMixin, TestCase):
    """Gluing the shard results into one document."""

    def test_shards_are_glued_in_order_with_volume_page_indexes(self):
        scan, rows = self.build(shard_count=3, pages_per_shard=2)

        key = surya.merge_surya_results(scan, rows)

        document = self.uploaded[key]
        self.assertEqual(key, surya.glued_result_key(scan, 1))
        self.assertEqual(
            [page["page_index"] for page in document["pages"]],
            [0, 1, 2, 3, 4, 5],
        )
        self.assertEqual(
            [page["pdf_page"] for page in document["pages"]],
            [1, 2, 3, 4, 5, 6],
        )
        self.assertEqual(
            [page["shard_index"] for page in document["pages"]],
            [0, 0, 1, 1, 2, 2],
        )
        self.assertEqual(document["source_page_count"], 6)
        self.assertEqual(document["engine"], "surya")
        self.assertEqual(document["dpi"], surya.DPI)
        self.assertEqual(document["source"], "original")
        self.assertEqual(len(document["shards"]), 3)

    def test_the_document_carries_no_raw(self):
        scan, rows = self.build(shard_count=1, pages_per_shard=2)

        document = self.uploaded[surya.merge_surya_results(scan, rows)]

        for page in document["pages"]:
            self.assertNotIn("raw", page)

    def test_a_page_that_failed_keeps_its_slot_and_its_error(self):
        scan, rows = self.build(
            shard_count=2,
            pages_per_shard=2,
            pages={1: [make_page(0), make_failed_page(1)]},
        )

        document = self.uploaded[surya.merge_surya_results(scan, rows)]

        self.assertEqual(len(document["pages"]), 4)
        self.assertIn("error", document["pages"][3])
        self.assertEqual(document["failed_pages"], [3])
        self.assertEqual(document["pages"][3]["page_index"], 3)

    def test_the_lists_are_in_volume_numbering(self):
        scan, rows = self.build(
            shard_count=2,
            pages_per_shard=2,
            pages={1: [make_empty_page(0), make_fallback_page(1)]},
        )

        document = self.uploaded[surya.merge_surya_results(scan, rows)]

        self.assertEqual(document["empty_pages"], [2])
        self.assertEqual(document["fallback_pages"], [3])
        self.assertEqual(document["failed_pages"], [])

    def test_the_derived_lists_agree_with_the_lists_of_the_payload(self):
        """The glue sorts the pages again rather than renumber the
        worker's lists, so the two must name the same pages. The
        payload's lists are the fixture's own arithmetic, and
        :class:`TestTheLostContentRule` is what holds the fixture, the
        glue and the worker to one answer."""
        shard = [
            make_page(0),
            make_failed_page(1),
            make_empty_page(2),
            make_fallback_page(3),
            make_dropped_page(4),
        ]
        scan, rows = self.build(
            shard_count=1, pages_per_shard=5, pages={0: shard}
        )
        payload = make_payload(shard)

        document = self.uploaded[surya.merge_surya_results(scan, rows)]

        for name, _member in surya.PAGE_LISTS:
            self.assertEqual(document[name], payload[name], name)

    def test_a_shard_that_answers_the_wrong_pages_raises(self):
        scan, rows = self.build(
            shard_count=1, pages_per_shard=2, pages={0: [make_page(0)]}
        )

        with self.assertRaises(surya.SuryaGlueError) as caught:
            surya.merge_surya_results(scan, rows)

        self.assertIn("the shard has 2", str(caught.exception))

    def test_a_run_of_no_rows_raises(self):
        scan = ScanFactory()

        with self.assertRaises(surya.SuryaGlueError):
            surya.merge_surya_results(scan, [])

    def test_a_document_that_does_not_upload_raises(self):
        scan, rows = self.build(shard_count=1, pages_per_shard=1)
        self.upload.side_effect = None
        self.upload.return_value = False

        with self.assertRaises(surya.SuryaGlueError):
            surya.merge_surya_results(scan, rows)

    def test_the_glue_is_idempotent(self):
        scan, rows = self.build(shard_count=2, pages_per_shard=2)

        first = self.uploaded[surya.merge_surya_results(scan, rows)]
        second = self.uploaded[surya.merge_surya_results(scan, rows)]

        self.assertEqual(first["pages"], second["pages"])


# ── the pass ────────────────────────────────────────────────────────
class TestFinishReadyRuns(SuryaRunMixin, TestCase):
    """The collect-tick pass over the finished runs."""

    def test_a_finished_run_is_glued_and_consumed(self):
        scan, rows = self.build(shard_count=2, pages_per_shard=2)

        self.assertEqual(surya.finish_ready_runs(), 1)

        for job in surya.live_extract_jobs(scan):
            self.assertEqual(job.status, JobStatus.CONSUMED)
            # The results are kept: the opinion glue and every re-glue
            # read them again.
            self.assertTrue(job.result_key)
        self.assertEqual(surya.glued_volume_key(scan), self.upload_key(scan))

    def upload_key(self, scan):
        """The key the run's document was uploaded to."""
        return surya.glued_result_key(scan, 1)

    def test_a_glued_run_is_not_glued_again(self):
        self.build(shard_count=1, pages_per_shard=1)

        self.assertEqual(surya.finish_ready_runs(), 1)
        self.assertEqual(surya.finish_ready_runs(), 0)

    def test_a_glued_run_hands_the_page_numbers_back(self):
        """The hand-back of #351, the twin of the Mistral glue's: a
        volume in review 1 whose dots.mocr run applied is read again."""
        scan, _rows = self.build(shard_count=1, pages_per_shard=1)
        Scan.objects.filter(pk=scan.pk).update(
            status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )
        rows = dots_mocr.ensure_analyze_jobs(scan, make_manifest(1, 1))
        ExternalJob.objects.filter(pk__in=[r.pk for r in rows]).update(
            status=JobStatus.CONSUMED
        )
        dots_mocr._write_apply_state(
            dots_mocr.live_analyze_jobs(scan), {"applied_at": "2026-09-23"}
        )

        with self.assertLogs("scanning.dots_mocr", level="INFO") as logs:
            self.assertEqual(surya.finish_ready_runs(), 1)

        self.assertEqual(
            dots_mocr._apply_state(dots_mocr.live_analyze_jobs(scan)), {}
        )
        self.assertIn("surya volume is glued", logs.output[-1])

    def test_the_pass_writes_no_scan_status(self):
        scan, _rows = self.build(shard_count=1, pages_per_shard=1)
        scan.status = Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        scan.save(update_fields=["status"])

        surya.finish_ready_runs()

        scan.refresh_from_db()
        self.assertEqual(
            scan.status, Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )

    def test_a_run_with_a_row_in_flight_waits(self):
        scan, rows = self.build(shard_count=2, pages_per_shard=1)
        ExternalJob.objects.filter(pk=rows[1].pk).update(
            status=JobStatus.SUBMITTED
        )

        self.assertEqual(surya.finish_ready_runs(), 0)

    def test_a_failing_glue_stops_after_three_attempts(self):
        scan, _rows = self.build(shard_count=1, pages_per_shard=1)
        with patch(
            "scanning.surya.merge_surya_results",
            side_effect=surya.SuryaGlueError("no"),
        ) as merge:
            for _ in range(surya.GLUE_MAX_ATTEMPTS + 2):
                self.assertEqual(surya.finish_ready_runs(), 0)

        self.assertEqual(merge.call_count, surya.GLUE_MAX_ATTEMPTS)
        self.assertEqual(
            surya._glue_attempts(surya.live_extract_jobs(scan)),
            surya.GLUE_MAX_ATTEMPTS,
        )

    def test_the_pass_needs_no_endpoint_id(self):
        """The results are paid for, so an endpoint id taken out of the
        environment must not leave them unglued."""
        self.build(shard_count=1, pages_per_shard=1)
        with patch("scanning.surya.enabled", return_value=False):
            self.assertEqual(surya.finish_ready_runs(), 1)
