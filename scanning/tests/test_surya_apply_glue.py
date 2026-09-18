"""Tests for the Surya read of a corrected volume (issue #368).

The twin of ``test_mistral_apply_glue``, because the stage is the twin:
Surya joins a built apply run (#224) long after it was built, since the
read starts by hand. So the rows of the edited pages are created by the
collect pass rather than by the build, and the document is written
outside ``apply.glues_due``. Under test:

- a row is created only for a volume somebody chose to read
- the document is the corrected volume's page space: a deleted page is
  gone, an edited page comes from its own result
- a run with no structural edit aliases the volume document
- a second read of the volume makes the document due again
- no review state waits for any of it
"""

import json
from unittest.mock import patch

from django.test import override_settings

from scanning import apply, surya
from scanning.models import (
    ApplyRun,
    ExternalJob,
    JobStage,
    JobStatus,
    PageEdit,
)
from scanning.tests.test_apply import MEDIA_ROOT
from scanning.tests.test_apply_glue import GlueTestCase, envelope
from scanning.tests.test_jobs import make_manifest
from scanning.tests.test_surya_glue import (
    make_failed_page,
    make_page,
    make_payload,
)

SURYA = {
    "RUNPOD_ENABLED": True,
    "RUNPOD_API_KEY": "key-1",
    "SURYA_ENABLED": True,
    "RUNPOD_SURYA_ENDPOINT_ID": "ep-surya",
    "MEDIA_ROOT": MEDIA_ROOT,
}


@override_settings(**SURYA)
class SuryaApplyTestCase(GlueTestCase):
    """A built run, a fake bucket, and a Surya read of the volume."""

    def setUp(self):
        """Answer this glue's reads of a shard result from the disk.

        The volume glue pulls a shard result through a temporary file
        (``s3_sync.download_object``), because a Surya result carries
        ``raw`` for every page. The shared fixture stores bytes under a
        key and JSON under another, so the download here writes
        whichever shape the key holds.
        """
        super().setUp()

        def download_object(key, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            stored = self.objects[key]
            if isinstance(stored, bytes):
                dest.write_bytes(stored)
            else:
                dest.write_text(json.dumps(stored))

        patcher = patch(
            "scanning.s3_sync.download_object", side_effect=download_object
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def volume_surya_run(self, text="volume"):
        """Glue a Surya volume run over the original's pages.

        :param text: The text every page reads.
        :returns: The glued document's key.
        """
        rows = surya.ensure_extract_jobs(
            self.scan, make_manifest(1, self.PAGES)
        )
        # The shard result the worker PUT, so the volume can be glued
        # again from the bucket (``reglue_surya_ocr``).
        shard_key = "result/surya/volume-s0"
        ExternalJob.objects.filter(pk__in=[r.pk for r in rows]).update(
            status=JobStatus.CONSUMED, result_key=shard_key
        )
        rows[0].result_key = shard_key
        self.objects[shard_key] = envelope(
            self.scan,
            rows[0],
            surya.ACTION,
            make_payload([make_page(n) for n in range(self.PAGES)]),
        )
        key = surya.glued_result_key(self.scan, rows[0].run)
        self.objects[key] = {
            "schema_version": surya.GLUE_SCHEMA_VERSION,
            "engine": "surya",
            "action": surya.ACTION,
            "scan_pk": self.scan.pk,
            "run": rows[0].run,
            "source_page_count": self.PAGES,
            "dpi": surya.DPI,
            "source": surya.SOURCE,
            "pages": [
                {
                    "page_index": p - 1,
                    "pdf_page": p,
                    "shard_index": 0,
                    "page_no": p - 1,
                    "origin_width": 1700,
                    "origin_height": 2200,
                    "text": f"{text} {p}",
                    "blocks": [],
                }
                for p in range(1, self.PAGES + 1)
            ],
            "failed_pages": [],
            "empty_pages": [],
            "fallback_pages": [],
            "dropped_block_pages": [],
        }
        return key

    def complete_surya_rows(self, run):
        """Answer every Surya row of the run with a stored result.

        Each page of an edit's shard reads as ``edit {pk} page {k}``.

        :param run: The built run.
        """
        for row in run.jobs.filter(stage=JobStage.EXTRACT, engine="surya"):
            key = f"result/surya/{row.pk}"
            edit_id = row.input_manifest["edit_id"]
            count = row.input_manifest["page_count"]
            ExternalJob.objects.filter(pk=row.pk).update(
                status=JobStatus.COMPLETED, result_key=key
            )
            row.result_key = key
            self.objects[key] = envelope(
                self.scan,
                row,
                surya.ACTION,
                make_payload(
                    [
                        make_page(k, text=f"edit {edit_id} page {k}")
                        for k in range(count)
                    ]
                ),
            )

    def read_run(self):
        """Build a run, read its edited pages, and glue the volume.

        :returns: ``(run, edits)``.
        """
        run, edits = self.built_run()
        self.volume_surya_run()
        surya.finish_ready_applies()
        run.refresh_from_db()
        self.complete_surya_rows(run)
        return run, edits


class TestRowCreation(SuryaApplyTestCase):
    """Who pays, and when."""

    def test_no_row_is_created_without_a_volume_read(self):
        run, _ = self.built_run()

        self.assertEqual(surya.finish_ready_applies(), 0)

        self.assertEqual(len(surya.apply_jobs(self.scan, run)), 0)

    def test_a_volume_read_gives_the_edited_pages_a_row_each(self):
        run, (turn, swap, leaf) = self.built_run()
        self.volume_surya_run()

        surya.finish_ready_applies()

        rows = surya.apply_jobs(self.scan, run)
        self.assertEqual(len(rows), 3)
        self.assertEqual(
            sorted(row.input_manifest["edit_id"] for row in rows),
            sorted([turn.pk, swap.pk, leaf.pk]),
        )
        self.assertEqual({row.status for row in rows}, {JobStatus.PENDING})

    def test_the_rows_name_the_shards_the_build_cut(self):
        run, (_turn, _swap, leaf) = self.built_run()
        self.volume_surya_run()

        surya.finish_ready_applies()

        rows = surya.apply_jobs(self.scan, run)
        row = next(r for r in rows if r.input_manifest["edit_id"] == leaf.pk)
        self.assertEqual(row.input_key, apply.page_shard_key(self.scan, leaf))
        self.assertEqual(row.input_manifest["page_count"], 2)
        self.assertEqual(row.apply_run_id, run.pk)

    def test_the_rows_are_created_once(self):
        run, _ = self.built_run()
        self.volume_surya_run()

        surya.finish_ready_applies()
        surya.finish_ready_applies()

        self.assertEqual(len(surya.apply_jobs(self.scan, run)), 3)

    def test_the_rows_do_not_collide_with_mistrals(self):
        """Both engines are ``EXTRACT``, so a row of one must never be
        read as a row of the other (#364)."""
        from scanning import mistral_ocr

        run, _ = self.built_run()
        self.volume_surya_run()
        surya.finish_ready_applies()

        self.assertEqual(len(surya.apply_jobs(self.scan, run)), 3)
        self.assertEqual(len(mistral_ocr.apply_jobs(self.scan, run)), 0)

    def test_an_identity_run_needs_no_row(self):
        self.edit(PageEdit.Kind.SET_NUMBER, pdf_page=2, value="12")
        run = apply.build_run(self.scan)
        self.volume_surya_run()

        self.assertEqual(surya.finish_ready_applies(), 1)

        self.assertEqual(len(surya.apply_jobs(self.scan, run)), 0)

    def test_the_pass_does_nothing_where_surya_is_off(self):
        run, _ = self.built_run()
        self.volume_surya_run()

        with override_settings(SURYA_ENABLED=False):
            self.assertEqual(surya.finish_ready_applies(), 0)

        self.assertEqual(len(surya.apply_jobs(self.scan, run)), 0)

    def test_an_open_volume_run_is_not_a_candidate(self):
        run, _ = self.built_run()
        rows = surya.ensure_extract_jobs(
            self.scan, make_manifest(1, self.PAGES)
        )
        ExternalJob.objects.filter(pk=rows[0].pk).update(
            status=JobStatus.SUBMITTED
        )

        self.assertEqual(surya.finish_ready_applies(), 0)

        self.assertEqual(len(surya.apply_jobs(self.scan, run)), 0)


class TestApplyGlue(SuryaApplyTestCase):
    """The corrected volume's own document."""

    def test_the_glue_waits_for_the_rows_it_created(self):
        run, _ = self.built_run()
        self.volume_surya_run()

        surya.finish_ready_applies()

        run.refresh_from_db()
        self.assertEqual(run.surya_key, "")
        self.assertIsNone(run.surya_run)

    def test_the_document_is_the_corrected_volume(self):
        run, _ = self.read_run()

        self.assertEqual(surya.finish_ready_applies(), 1)

        run.refresh_from_db()
        document = self.objects[run.surya_key]
        self.assertEqual(
            run.surya_key,
            f"{apply.run_prefix(self.scan, run)}surya-volume.json",
        )
        self.assertEqual(document["apply_run"], run.label)
        self.assertEqual(document["source_page_count"], 7)
        self.assertEqual(
            [page["pdf_page"] for page in document["pages"]],
            list(range(1, 8)),
        )
        self.assertEqual(
            [page["page_index"] for page in document["pages"]],
            list(range(7)),
        )
        self.assertEqual(document["dpi"], surya.DPI)
        self.assertEqual(document["source"], surya.SOURCE)

    def test_a_deleted_page_is_gone_and_the_kept_pages_move_up(self):
        run, _ = self.read_run()

        surya.finish_ready_applies()

        run.refresh_from_db()
        pages = self.objects[run.surya_key]["pages"]
        # The original's page 2 was deleted, so no page reads "volume 2".
        self.assertNotIn("volume 2", [page["text"] for page in pages])
        self.assertEqual(pages[0]["text"], "volume 1")
        self.assertEqual(pages[0]["source"]["kind"], "original")
        self.assertEqual(pages[0]["source"]["pdf_page"], 1)

    def test_an_edited_page_comes_from_its_own_read(self):
        run, (turn, swap, leaf) = self.read_run()

        surya.finish_ready_applies()

        run.refresh_from_db()
        pages = self.objects[run.surya_key]["pages"]
        by_edit = {
            (page["source"].get("edit_id"), page["source"].get("page")): page
            for page in pages
            if page["source"]["kind"] == "edit"
        }
        self.assertEqual(
            by_edit[(leaf.pk, 0)]["text"], f"edit {leaf.pk} page 0"
        )
        self.assertEqual(
            by_edit[(leaf.pk, 1)]["text"], f"edit {leaf.pk} page 1"
        )
        self.assertEqual(
            by_edit[(swap.pk, 0)]["text"], f"edit {swap.pk} page 0"
        )
        self.assertEqual(
            by_edit[(turn.pk, 0)]["text"], f"edit {turn.pk} page 0"
        )
        # The block boxes of the edited page reach the document.
        self.assertEqual(len(by_edit[(swap.pk, 0)]["blocks"]), 1)
        # The shard's own numbering never reaches the document.
        self.assertNotIn("page_no", by_edit[(leaf.pk, 1)])
        self.assertNotIn("shard_index", by_edit[(leaf.pk, 1)])
        # The whole answer stays in the shard result.
        self.assertNotIn("raw", by_edit[(leaf.pk, 1)])

    def test_an_identity_run_aliases_the_volume_document(self):
        self.edit(PageEdit.Kind.SET_NUMBER, pdf_page=2, value="12")
        run = apply.build_run(self.scan)
        volume_key = self.volume_surya_run()

        surya.finish_ready_applies()

        run.refresh_from_db()
        self.assertEqual(run.surya_key, volume_key)
        self.assertEqual(run.surya_run, 1)

    def test_the_rows_are_consumed_and_their_results_kept(self):
        run, _ = self.read_run()

        surya.finish_ready_applies()

        rows = surya.apply_jobs(self.scan, run)
        self.assertEqual({row.status for row in rows}, {JobStatus.CONSUMED})
        for row in rows:
            self.assertIn(row.result_key, self.objects)

    def test_the_pass_costs_three_queries_when_nothing_owes_anything(self):
        """Every volume ever read keeps a glued run for good, so the
        pre-check must not grow with the corpus (#245)."""
        self.read_run()
        surya.finish_ready_applies()

        with self.assertNumQueries(3):
            self.assertEqual(surya.finish_ready_applies(), 0)

    def test_a_run_missing_a_row_for_one_edit_is_not_glued(self):
        """Every edited page needs a row before the document is
        written: a hole here would be stamped as done."""
        run, (_turn, _swap, leaf) = self.read_run()
        ExternalJob.objects.filter(
            apply_run=run,
            stage=JobStage.EXTRACT,
            engine="surya",
            input_manifest__edit_id=leaf.pk,
        ).delete()
        rows = surya.apply_jobs(self.scan, run)

        self.assertFalse(surya.apply_glue_due(run, rows, 1))
        self.assertFalse(surya.apply_glue_due(run, rows, 1, force=True))

    def test_a_run_with_only_a_deletion_needs_no_row(self):
        """Not an identity run, and still nothing to read."""
        self.edit(PageEdit.Kind.DELETE_PAGE, pdf_page=2)
        run = apply.build_run(self.scan)
        self.volume_surya_run()

        self.assertEqual(surya.finish_ready_applies(), 1)

        run.refresh_from_db()
        self.assertEqual(len(surya.apply_jobs(self.scan, run)), 0)
        pages = self.objects[run.surya_key]["pages"]
        self.assertEqual(len(pages), self.PAGES - 1)
        self.assertEqual([p for p in pages if "error" in p], [])

    def test_the_document_is_written_once(self):
        self.read_run()

        self.assertEqual(surya.finish_ready_applies(), 1)
        self.assertEqual(surya.finish_ready_applies(), 0)

    def test_a_second_volume_read_makes_the_document_due_again(self):
        run, _ = self.read_run()
        surya.finish_ready_applies()
        run.refresh_from_db()
        first = run.surya_key

        # A second read of the volume: a later run, glued.
        rows = surya.ensure_extract_jobs(
            self.scan, make_manifest(1, self.PAGES), force_new_run=True
        )
        ExternalJob.objects.filter(pk__in=[r.pk for r in rows]).update(
            status=JobStatus.CONSUMED
        )
        key = surya.glued_result_key(self.scan, rows[0].run)
        self.objects[key] = dict(
            self.objects[surya.glued_result_key(self.scan, 1)],
            run=rows[0].run,
        )

        self.assertEqual(surya.finish_ready_applies(), 1)

        run.refresh_from_db()
        self.assertEqual(run.surya_key, first)
        self.assertEqual(run.surya_run, rows[0].run)

    def test_a_missing_volume_page_is_a_failure_that_is_counted(self):
        run, _ = self.read_run()
        volume_key = surya.glued_result_key(self.scan, 1)
        self.objects[volume_key] = dict(
            self.objects[volume_key],
            pages=self.objects[volume_key]["pages"][:1],
        )

        self.assertEqual(surya.finish_ready_applies(), 0)

        run.refresh_from_db()
        self.assertEqual(run.surya_key, "")
        head = surya.live_extract_jobs(self.scan)[0]
        self.assertEqual(
            head.provider_meta[f"glue:{run.label}"]["attempts"],
            1,
            "one ledger per corrected volume, beside the volume glue's",
        )
        self.assertNotIn("glue", head.provider_meta)

    def test_a_page_the_read_could_not_answer_keeps_its_slot(self):
        """A hole must not shift the pages after it: it keeps its slot
        and says so, in the corrected volume's own numbering."""
        run, (_turn, _swap, leaf) = self.read_run()
        row = next(
            r
            for r in surya.apply_jobs(self.scan, run)
            if r.input_manifest["edit_id"] == leaf.pk
        )
        # The second page of the inserted leaf came back with no block.
        self.objects[row.result_key] = envelope(
            self.scan,
            row,
            surya.ACTION,
            make_payload(
                [
                    make_page(0, text=f"edit {leaf.pk} page 0"),
                    make_failed_page(1),
                ]
            ),
        )

        surya.finish_ready_applies()

        run.refresh_from_db()
        document = self.objects[run.surya_key]
        self.assertEqual(len(document["pages"]), 7)
        self.assertEqual(document["failed_pages"], [5])
        hole = document["pages"][5]
        self.assertEqual(hole["source"]["edit_id"], leaf.pk)
        self.assertEqual(hole["source"]["page"], 1)
        self.assertEqual(hole["pdf_page"], 6)


class TestNoReviewStateWaits(SuryaApplyTestCase):
    """The Surya outputs gate nothing (#368)."""

    def test_a_complete_run_needs_no_surya_key(self):
        run, _ = self.built_run()
        ApplyRun.objects.filter(pk=run.pk).update(
            bitonal_key="b",
            ocr_key="o",
            printed_pages_key="p",
            detections_key="d",
        )
        run.refresh_from_db()

        self.assertEqual(run.surya_key, "")
        self.assertTrue(run.is_complete)

    def test_the_apply_owes_no_phase_for_the_surya_document(self):
        """The corrected volume is finished without it: an arm in
        ``glues_due`` would wait for a read that starts later than the
        status the trigger takes."""
        self.read_run()
        with patch("scanning.apply.glues_due", return_value=[]):
            self.assertIsNone(apply.phase_due(self.scan))

    def test_the_pass_writes_no_scan_status(self):
        self.read_run()

        surya.finish_ready_applies()

        before = self.scan.status
        self.scan.refresh_from_db()
        self.assertEqual(self.scan.status, before)
