"""Tests for the Mistral glue (issue #245).

S3 is inert under ``TESTING``, so the glue's download answers from a
dict of stored envelopes and its upload is captured. Under test:

- the parse, the one transform of a stored result: the body of a line,
  the blocks, the coordinate markers, and a hole that keeps its slot
- the page arithmetic: shard-local ``page_no`` to volume ``page_index``
- the finish pass: rows consumed, results kept, no scan status written,
  and a bounded retry of a glue that keeps failing
- the corrected volume: the deleted pages gone, the edited pages read
  from their own results, and the rows created only for a volume a
  person chose to read
"""

from unittest.mock import patch

from django.test import override_settings

from scanning import jobs, mistral_ocr, s3_sync
from scanning.factories import ScanFactory
from scanning.models import (
    ExternalJob,
    JobStatus,
)
from scanning.tests.test_jobs import make_manifest
from scanning.tests.test_mistral_ocr import MISTRAL
from scanning.tests.test_views import ScanningTestCase


def make_line(page_no: int, text: str = "878 N. C.") -> dict:
    """Build one output line of the shape Mistral writes.

    The same shape as the fixture of ``test_mistral_ocr.TestSweep``:
    one request answered one page image, and the block list is the
    page's own.

    :param page_no: 0-based page inside the shard.
    :param text: The block's text.
    :returns: One output line.
    :rtype: dict
    """
    return {
        "id": f"line-{page_no}",
        "custom_id": mistral_ocr.custom_id(page_no),
        "response": {
            "status_code": 200,
            "body": {
                "pages": [
                    {
                        "index": 0,
                        "markdown": f"# {text}",
                        "images": [],
                        "dimensions": {
                            "dpi": 200,
                            "height": 2200,
                            "width": 1700,
                        },
                        "blocks": [
                            {
                                "type": "text",
                                "bbox": {
                                    "top_left_x": 100,
                                    "top_left_y": 200,
                                    "bottom_right_x": 900,
                                    "bottom_right_y": 300,
                                },
                                "content": text,
                            }
                        ],
                    }
                ],
                "usage_info": {"pages_processed": 1},
                "model": "mistral-ocr-latest",
            },
        },
        "error": None,
    }


def make_payload(page_count: int, failed: tuple[int, ...] = ()) -> dict:
    """Build the payload the harvest stores for one shard.

    :param page_count: Pages the shard covers.
    :param failed: The pages no line answers.
    :returns: The payload.
    :rtype: dict
    """
    return {
        "output": [
            make_line(page_no)
            for page_no in range(page_count)
            if page_no not in failed
        ],
        "errors": [
            {"custom_id": mistral_ocr.custom_id(page_no), "error": {"m": "x"}}
            for page_no in failed
        ],
        "batch": {"id": "batch-1"},
        "model": "mistral-ocr-latest",
        "render": {"width": 1700, "height": 2200, "source": "original"},
        "page_count": page_count,
        "failed_pages": list(failed),
    }


def make_envelope(job: ExternalJob, payload: dict, **overrides) -> dict:
    """Build the result envelope the harvest PUTs to S3.

    :param job: The row the envelope answers.
    :param payload: The stored payload.
    :param overrides: Envelope fields to replace, for the check tests.
    :returns: An envelope dict.
    :rtype: dict
    """
    envelope = {
        "schema_version": jobs.RESULT_SCHEMA_VERSION,
        "action": "extract",
        "scan_pk": job.scan_id,
        "result_key": job.result_key,
        "payload": payload,
    }
    envelope.update(overrides)
    return envelope


class MistralRunMixin:
    """Builds a scan whose Mistral rows have results on 'S3'."""

    def build(self, shard_count=3, pages_per_shard=2, failed=()):
        """Create a scan, its rows, and an envelope per shard.

        :param shard_count: Shards to create.
        :param pages_per_shard: Pages each shard covers.
        :param failed: Shard-local pages no line answers, in shard 0.
        :returns: ``(scan, rows)``.
        """
        self.store: dict[str, dict] = {}
        scan = ScanFactory(page_count=shard_count * pages_per_shard)
        rows = mistral_ocr.ensure_extract_jobs(
            scan, make_manifest(shard_count, pages_per_shard)
        )
        for index, job in enumerate(rows):
            job.status = JobStatus.COMPLETED
            job.result_key = f"jobs/extract/mistral_ocr/r1-s{index}-a1.json"
            job.save()
            self.store[job.result_key] = make_envelope(
                job,
                make_payload(
                    pages_per_shard, failed=failed if index == 0 else ()
                ),
            )
        self._patch_s3()
        return scan, mistral_ocr.live_extract_jobs(scan)

    def _patch_s3(self):
        """Answer the glue's reads from :attr:`store`, capture its writes."""
        download = patch(
            "scanning.s3_sync.download_json_object",
            side_effect=lambda key: self.store[key],
        )
        self.download = download.start()
        self.addCleanup(download.stop)
        active = patch("scanning.s3_sync.s3_active", return_value=True)
        active.start()
        self.addCleanup(active.stop)

        def _upload(key, document):
            self.store[key] = document
            return True

        upload = patch(
            "scanning.s3_sync.upload_json_object", side_effect=_upload
        )
        self.upload = upload.start()
        self.addCleanup(upload.stop)


# ── the parse ───────────────────────────────────────────────────────
class TestParsePayload(ScanningTestCase):
    """The one transform of a stored result."""

    def test_a_page_carries_its_markdown_and_its_blocks(self):
        pages = mistral_ocr.parse_payload(make_payload(1))

        self.assertEqual(sorted(pages), [0])
        page = pages[0]
        self.assertEqual(page["page_no"], 0)
        self.assertEqual(page["md"], "# 878 N. C.")
        self.assertEqual(page["dimensions"]["width"], 1700)
        self.assertEqual(
            page["blocks"],
            [
                {
                    "id": 0,
                    "type": "text",
                    "bbox": {
                        "top_left_x": 100,
                        "top_left_y": 200,
                        "bottom_right_x": 900,
                        "bottom_right_y": 300,
                    },
                    "content": "878 N. C.",
                }
            ],
        )
        self.assertNotIn("error", page)

    def test_every_page_of_the_shard_comes_back_in_order(self):
        pages = mistral_ocr.parse_payload(make_payload(4))

        self.assertEqual(sorted(pages), [0, 1, 2, 3])
        self.assertEqual([p["page_no"] for p in pages.values()], [0, 1, 2, 3])

    def test_a_page_no_line_answers_keeps_its_slot_with_an_error(self):
        pages = mistral_ocr.parse_payload(make_payload(3, failed=(1,)))

        self.assertEqual(sorted(pages), [0, 1, 2])
        self.assertIn("error", pages[1])
        self.assertEqual(pages[1]["md"], "")
        self.assertEqual(pages[1]["blocks"], [])
        self.assertNotIn("error", pages[0])
        self.assertNotIn("error", pages[2])

    def test_a_line_with_an_error_is_a_hole(self):
        payload = make_payload(1)
        payload["output"][0]["error"] = {"message": "rate limited"}

        pages = mistral_ocr.parse_payload(payload)

        self.assertIn("error", pages[0])

    def test_a_line_with_no_body_is_a_hole(self):
        payload = make_payload(1)
        payload["output"][0]["response"] = None

        pages = mistral_ocr.parse_payload(payload)

        self.assertIn("error", pages[0])

    def test_a_line_whose_body_has_no_page_is_a_hole(self):
        payload = make_payload(1)
        payload["output"][0]["response"]["body"]["pages"] = []

        pages = mistral_ocr.parse_payload(payload)

        self.assertIn("error", pages[0])

    def test_a_custom_id_this_stage_did_not_mint_is_ignored(self):
        payload = make_payload(1)
        payload["output"].append({"custom_id": "x9", "response": {"body": {}}})

        pages = mistral_ocr.parse_payload(payload)

        self.assertEqual(sorted(pages), [0])

    def test_the_block_text_is_read_under_either_name(self):
        """``content`` is what the glue writes; Mistral has been seen to
        answer under either name, and the key is internal."""
        payload = make_payload(1)
        block = payload["output"][0]["response"]["body"]["pages"][0]["blocks"][
            0
        ]
        del block["content"]
        block["text"] = "read me"

        pages = mistral_ocr.parse_payload(payload)

        self.assertEqual(pages[0]["blocks"][0]["content"], "read me")

    def test_the_leaked_coordinate_markers_come_off_the_text(self):
        payload = make_payload(1)
        block = payload["output"][0]["response"]["body"]["pages"][0]["blocks"][
            0
        ]
        block["content"] = (
            "[BBOX]0.1,0.2,0.9,0.3[/BBOX]The defendant was indicted"
        )

        pages = mistral_ocr.parse_payload(payload)

        self.assertEqual(
            pages[0]["blocks"][0]["content"], "The defendant was indicted"
        )

    def test_a_marker_with_no_closing_tag_takes_no_sentence_with_it(self):
        payload = make_payload(1)
        block = payload["output"][0]["response"]["body"]["pages"][0]["blocks"][
            0
        ]
        block["content"] = "[BBOX]0.1,0.2,0.9,0.3 The defendant was indicted"

        pages = mistral_ocr.parse_payload(payload)

        self.assertEqual(
            pages[0]["blocks"][0]["content"], "The defendant was indicted"
        )

    def test_a_page_with_no_block_list_reads_as_no_blocks(self):
        payload = make_payload(1)
        del payload["output"][0]["response"]["body"]["pages"][0]["blocks"]

        pages = mistral_ocr.parse_payload(payload)

        self.assertEqual(pages[0]["blocks"], [])
        self.assertNotIn("error", pages[0])


# ── the volume glue ─────────────────────────────────────────────────
@override_settings(**MISTRAL)
class TestMergeExtractResults(MistralRunMixin, ScanningTestCase):
    """Gluing the shard results into the volume document."""

    def test_shards_are_glued_in_order_with_volume_page_indexes(self):
        scan, rows = self.build(shard_count=3, pages_per_shard=2)

        key = mistral_ocr.merge_extract_results(scan, rows)

        document = self.store[key]
        self.assertEqual(
            [page["page_index"] for page in document["pages"]],
            list(range(6)),
        )
        self.assertEqual(
            [page["pdf_page"] for page in document["pages"]],
            list(range(1, 7)),
        )
        self.assertEqual(
            [page["shard_index"] for page in document["pages"]],
            [0, 0, 1, 1, 2, 2],
        )

    def test_the_document_names_the_run_the_model_and_the_render(self):
        scan, rows = self.build(shard_count=2, pages_per_shard=2)

        key = mistral_ocr.merge_extract_results(scan, rows)

        document = self.store[key]
        self.assertEqual(
            document["schema_version"], mistral_ocr.GLUE_SCHEMA_VERSION
        )
        self.assertEqual(document["engine"], "mistral_ocr")
        self.assertEqual(document["action"], "extract")
        self.assertEqual(document["scan_pk"], scan.pk)
        self.assertEqual(document["run"], 1)
        self.assertEqual(document["source_page_count"], 4)
        self.assertEqual(document["model"], "mistral-ocr-latest")
        self.assertEqual(
            document["render"],
            {
                "width": mistral_ocr.RENDER_W,
                "height": mistral_ocr.RENDER_H,
                "source": mistral_ocr.SOURCE,
            },
        )
        self.assertEqual(document["failed_pages"], [])

    def test_the_document_lands_at_a_run_scoped_key(self):
        scan, rows = self.build(shard_count=2, pages_per_shard=1)

        key = mistral_ocr.merge_extract_results(scan, rows)

        self.assertEqual(
            key,
            f"{s3_sync.s3_processing_prefix(scan)}"
            f"jobs/extract/mistral_ocr/r1-volume.json",
        )

    def test_a_hole_is_listed_in_volume_numbering(self):
        scan, rows = self.build(shard_count=2, pages_per_shard=2, failed=(1,))

        key = mistral_ocr.merge_extract_results(scan, rows)

        document = self.store[key]
        self.assertEqual(document["failed_pages"], [1])
        self.assertIn("error", document["pages"][1])
        self.assertEqual(len(document["pages"]), 4)

    def test_shard_provenance_is_recorded(self):
        scan, rows = self.build(shard_count=2, pages_per_shard=3)

        key = mistral_ocr.merge_extract_results(scan, rows)

        shards = self.store[key]["shards"]
        self.assertEqual(len(shards), 2)
        self.assertEqual(shards[0]["from_page"], 0)
        self.assertEqual(shards[1]["from_page"], 3)
        self.assertEqual(shards[1]["page_count"], 3)
        self.assertEqual(shards[1]["result_key"], rows[1].result_key)
        self.assertEqual(shards[1]["model"], "mistral-ocr-latest")

    def test_a_second_glue_writes_the_same_document(self):
        scan, rows = self.build(shard_count=2, pages_per_shard=2)

        first = self.store[mistral_ocr.merge_extract_results(scan, rows)]
        first.pop("generated_at")
        second = self.store[mistral_ocr.merge_extract_results(scan, rows)]
        second.pop("generated_at")

        self.assertEqual(first, second)

    def test_a_run_with_no_rows_is_refused(self):
        with self.assertRaises(mistral_ocr.MistralGlueError):
            mistral_ocr.merge_extract_results(ScanFactory(), [])

    def test_a_shard_with_no_result_key_is_refused(self):
        scan, rows = self.build(shard_count=2, pages_per_shard=1)
        ExternalJob.objects.filter(pk=rows[1].pk).update(result_key="")

        with self.assertRaises(mistral_ocr.MistralGlueError) as caught:
            mistral_ocr.merge_extract_results(
                scan, mistral_ocr.live_extract_jobs(scan)
            )

        self.assertIn("no result key", str(caught.exception))

    def test_a_broken_shard_sequence_is_refused(self):
        scan, rows = self.build(shard_count=2, pages_per_shard=1)

        with self.assertRaises(mistral_ocr.MistralGlueError) as caught:
            mistral_ocr.merge_extract_results(scan, [rows[1], rows[0]])

        self.assertIn("sequence breaks", str(caught.exception))

    def test_an_envelope_of_another_action_is_refused(self):
        scan, rows = self.build(shard_count=1, pages_per_shard=1)
        self.store[rows[0].result_key]["action"] = "parse"

        with self.assertRaises(mistral_ocr.MistralGlueError):
            mistral_ocr.merge_extract_results(scan, rows)

    def test_a_shard_that_answered_the_wrong_page_count_is_refused(self):
        scan, rows = self.build(shard_count=1, pages_per_shard=2)
        self.store[rows[0].result_key]["payload"]["page_count"] = 1

        with self.assertRaises(mistral_ocr.MistralGlueError) as caught:
            mistral_ocr.merge_extract_results(scan, rows)

        self.assertIn("answered page(s)", str(caught.exception))

    def test_a_failed_upload_is_refused(self):
        scan, rows = self.build(shard_count=1, pages_per_shard=1)
        self.upload.side_effect = None
        self.upload.return_value = False

        with self.assertRaises(mistral_ocr.MistralGlueError) as caught:
            mistral_ocr.merge_extract_results(scan, rows)

        self.assertIn("could not be uploaded", str(caught.exception))


# ── the finish pass ─────────────────────────────────────────────────
@override_settings(**MISTRAL)
class TestFinishReadyRuns(MistralRunMixin, ScanningTestCase):
    """The collect-tick pass over the finished runs."""

    def test_a_finished_run_is_glued_and_consumed(self):
        scan, rows = self.build(shard_count=2, pages_per_shard=1)

        self.assertEqual(mistral_ocr.finish_ready_runs(), 1)

        self.assertEqual(
            [row.status for row in mistral_ocr.live_extract_jobs(scan)],
            [JobStatus.CONSUMED, JobStatus.CONSUMED],
        )
        self.assertIn(mistral_ocr.glued_result_key(scan, 1), self.store)

    def test_the_pass_writes_no_scan_status(self):
        scan, _rows = self.build(shard_count=1, pages_per_shard=1)
        before = scan.status

        mistral_ocr.finish_ready_runs()

        scan.refresh_from_db()
        self.assertEqual(scan.status, before)

    def test_the_shard_results_are_kept(self):
        scan, rows = self.build(shard_count=1, pages_per_shard=1)

        mistral_ocr.finish_ready_runs()

        row = mistral_ocr.live_extract_jobs(scan)[0]
        self.assertEqual(row.result_key, rows[0].result_key)
        self.assertIn(row.result_key, self.store)

    def test_a_glued_run_is_not_glued_again(self):
        self.build(shard_count=1, pages_per_shard=1)

        self.assertEqual(mistral_ocr.finish_ready_runs(), 1)
        self.assertEqual(mistral_ocr.finish_ready_runs(), 0)

    def test_an_open_run_waits(self):
        scan, rows = self.build(shard_count=2, pages_per_shard=1)
        ExternalJob.objects.filter(pk=rows[1].pk).update(
            status=JobStatus.SUBMITTED
        )

        self.assertEqual(mistral_ocr.finish_ready_runs(), 0)

        self.assertNotIn(mistral_ocr.glued_result_key(scan, 1), self.store)

    def test_a_dead_row_holds_the_run(self):
        scan, rows = self.build(shard_count=2, pages_per_shard=1)
        ExternalJob.objects.filter(pk=rows[1].pk).update(
            status=JobStatus.FAILED
        )

        self.assertEqual(mistral_ocr.finish_ready_runs(), 0)

    def test_a_failing_glue_is_counted_and_then_left_alone(self):
        scan, rows = self.build(shard_count=1, pages_per_shard=1)
        self.store[rows[0].result_key]["action"] = "parse"

        for _ in range(mistral_ocr.GLUE_MAX_ATTEMPTS + 2):
            self.assertEqual(mistral_ocr.finish_ready_runs(), 0)

        head = mistral_ocr.live_extract_jobs(scan)[0]
        state = head.provider_meta["glue"]["glue"]
        self.assertEqual(state["attempts"], mistral_ocr.GLUE_MAX_ATTEMPTS)
        self.assertIn("envelope", state["last_error"])

    def test_the_ledger_stays_out_of_the_input_manifest(self):
        """``jobs._still_describes`` compares the manifest exactly, so a
        counter there would read as a stale run."""
        scan, rows = self.build(shard_count=1, pages_per_shard=1)
        before = dict(rows[0].input_manifest)
        self.store[rows[0].result_key]["action"] = "parse"

        mistral_ocr.finish_ready_runs()

        head = mistral_ocr.live_extract_jobs(scan)[0]
        self.assertEqual(head.input_manifest, before)

    def test_nothing_is_glued_without_s3(self):
        self.build(shard_count=1, pages_per_shard=1)
        with patch("scanning.s3_sync.s3_active", return_value=False):
            self.assertEqual(mistral_ocr.finish_ready_runs(), 0)

    def test_the_glued_volume_key_follows_the_rows(self):
        scan, _rows = self.build(shard_count=1, pages_per_shard=1)

        self.assertIsNone(mistral_ocr.glued_volume_key(scan))
        mistral_ocr.finish_ready_runs()
        self.assertEqual(
            mistral_ocr.glued_volume_key(scan),
            mistral_ocr.glued_result_key(scan, 1),
        )
