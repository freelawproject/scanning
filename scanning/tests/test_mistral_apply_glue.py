"""Tests for the Mistral read of a corrected volume (issue #245).

The stage joins a built apply run (#224) long after it was built: the
read starts by hand today, and after the second review once #336
lands. So the rows of the edited pages are created by the collect pass
rather than by the build, and the document is written outside
``apply.glues_due``. Under test:

- a row is created only for a volume somebody chose to read
- the document is the corrected volume's page space: a deleted page is
  gone, an edited page comes from its own result
- a run with no structural edit aliases the volume document
- a second read of the volume makes the document due again
- no review state waits for any of it
"""

from unittest.mock import patch

from django.test import override_settings

from scanning import apply, mistral_ocr
from scanning.models import (
    ApplyRun,
    ExternalJob,
    JobStage,
    JobStatus,
    PageEdit,
    Status,
)
from scanning.tests.test_apply import MEDIA_ROOT
from scanning.tests.test_apply_glue import GlueTestCase, envelope
from scanning.tests.test_jobs import make_manifest
from scanning.tests.test_mistral_glue import make_line, make_payload

MISTRAL = {
    "MISTRAL_API_KEY": "key-1",
    "MISTRAL_MODEL": "mistral-ocr-latest",
    "MEDIA_ROOT": MEDIA_ROOT,
}


@override_settings(**MISTRAL)
class MistralApplyTestCase(GlueTestCase):
    """A built run, a fake bucket, and a Mistral read of the volume."""

    def volume_extract_run(self, text="volume"):
        """Glue a Mistral volume run over the original's pages.

        :param text: The text every page reads.
        :returns: The glued document's key.
        """
        rows = mistral_ocr.ensure_extract_jobs(
            self.scan, make_manifest(1, self.PAGES)
        )
        # The shard result the harvest stored, so the volume can be
        # glued again from the bucket (``reglue_mistral_ocr``).
        shard_key = "result/extract/volume-s0"
        ExternalJob.objects.filter(pk__in=[r.pk for r in rows]).update(
            status=JobStatus.CONSUMED, result_key=shard_key
        )
        rows[0].result_key = shard_key
        self.objects[shard_key] = envelope(
            self.scan,
            rows[0],
            mistral_ocr.ACTION,
            make_payload(self.PAGES),
        )
        key = mistral_ocr.glued_result_key(self.scan, rows[0].run)
        self.objects[key] = {
            "schema_version": mistral_ocr.GLUE_SCHEMA_VERSION,
            "engine": "mistral_ocr",
            "action": mistral_ocr.ACTION,
            "scan_pk": self.scan.pk,
            "run": rows[0].run,
            "source_page_count": self.PAGES,
            "model": "mistral-ocr-latest",
            "render": {"width": 1700, "height": 2200, "source": "original"},
            "pages": [
                {
                    "page_index": p - 1,
                    "pdf_page": p,
                    "shard_index": 0,
                    "page_no": p - 1,
                    "md": f"{text} {p}",
                    "blocks": [],
                }
                for p in range(1, self.PAGES + 1)
            ],
            "failed_pages": [],
        }
        return key

    def complete_extract_rows(self, run):
        """Answer every Mistral row of the run with a stored result.

        Each page of an edit's shard reads as ``edit {pk} page {k}``.

        :param run: The built run.
        """
        for row in run.jobs.filter(stage=JobStage.EXTRACT):
            key = f"result/extract/{row.pk}"
            edit_id = row.input_manifest["edit_id"]
            count = row.input_manifest["page_count"]
            ExternalJob.objects.filter(pk=row.pk).update(
                status=JobStatus.COMPLETED, result_key=key
            )
            row.result_key = key
            payload = make_payload(count)
            payload["output"] = [
                make_line(k, text=f"edit {edit_id} page {k}")
                for k in range(count)
            ]
            self.objects[key] = envelope(
                self.scan, row, mistral_ocr.ACTION, payload
            )

    def read_run(self):
        """Build a run, read its edited pages, and glue the volume.

        :returns: ``(run, edits)``.
        """
        run, edits = self.built_run()
        self.volume_extract_run()
        mistral_ocr.finish_ready_applies()
        run.refresh_from_db()
        self.complete_extract_rows(run)
        return run, edits


class TestRowCreation(MistralApplyTestCase):
    """Who pays, and when."""

    def test_no_row_is_created_without_a_volume_read(self):
        run, _ = self.built_run()

        self.assertEqual(mistral_ocr.finish_ready_applies(), 0)

        self.assertEqual(run.jobs.filter(stage=JobStage.EXTRACT).count(), 0)

    def test_a_volume_read_gives_the_edited_pages_a_row_each(self):
        run, (turn, swap, leaf) = self.built_run()
        self.volume_extract_run()

        mistral_ocr.finish_ready_applies()

        rows = mistral_ocr.apply_jobs(self.scan, run)
        self.assertEqual(len(rows), 3)
        self.assertEqual(
            sorted(row.input_manifest["edit_id"] for row in rows),
            sorted([turn.pk, swap.pk, leaf.pk]),
        )
        self.assertEqual({row.status for row in rows}, {JobStatus.PENDING})

    def test_the_rows_name_the_shards_the_build_cut(self):
        run, (_turn, _swap, leaf) = self.built_run()
        self.volume_extract_run()

        mistral_ocr.finish_ready_applies()

        rows = mistral_ocr.apply_jobs(self.scan, run)
        row = next(r for r in rows if r.input_manifest["edit_id"] == leaf.pk)
        self.assertEqual(row.input_key, apply.page_shard_key(self.scan, leaf))
        self.assertEqual(row.input_manifest["page_count"], 2)
        self.assertEqual(row.apply_run_id, run.pk)

    def test_the_rows_are_created_once(self):
        run, _ = self.built_run()
        self.volume_extract_run()

        mistral_ocr.finish_ready_applies()
        mistral_ocr.finish_ready_applies()

        self.assertEqual(len(mistral_ocr.apply_jobs(self.scan, run)), 3)

    def test_an_identity_run_needs_no_row(self):
        self.edit(PageEdit.Kind.SET_NUMBER, pdf_page=2, value="12")
        run = apply.build_run(self.scan)
        self.volume_extract_run()

        self.assertEqual(mistral_ocr.finish_ready_applies(), 1)

        self.assertEqual(run.jobs.filter(stage=JobStage.EXTRACT).count(), 0)

    def test_the_pass_does_nothing_where_mistral_is_off(self):
        run, _ = self.built_run()
        self.volume_extract_run()

        with override_settings(MISTRAL_API_KEY=""):
            self.assertEqual(mistral_ocr.finish_ready_applies(), 0)

        self.assertEqual(run.jobs.filter(stage=JobStage.EXTRACT).count(), 0)

    def test_an_open_volume_run_is_not_a_candidate(self):
        run, _ = self.built_run()
        rows = mistral_ocr.ensure_extract_jobs(
            self.scan, make_manifest(1, self.PAGES)
        )
        ExternalJob.objects.filter(pk=rows[0].pk).update(
            status=JobStatus.SUBMITTED
        )

        self.assertEqual(mistral_ocr.finish_ready_applies(), 0)

        self.assertEqual(run.jobs.filter(stage=JobStage.EXTRACT).count(), 0)


class TestApplyGlue(MistralApplyTestCase):
    """The corrected volume's own document."""

    def test_the_glue_waits_for_the_rows_it_created(self):
        run, _ = self.built_run()
        self.volume_extract_run()

        mistral_ocr.finish_ready_applies()

        run.refresh_from_db()
        self.assertEqual(run.extract_key, "")
        self.assertIsNone(run.extract_run)

    def test_the_document_is_the_corrected_volume(self):
        run, (turn, swap, leaf) = self.read_run()

        self.assertEqual(mistral_ocr.finish_ready_applies(), 1)

        run.refresh_from_db()
        document = self.objects[run.extract_key]
        self.assertEqual(
            run.extract_key,
            f"{apply.run_prefix(self.scan, run)}extract-volume.json",
        )
        self.assertEqual(document["apply_run"], run.label)
        self.assertEqual(document["source_page_count"], 7)
        self.assertEqual(len(document["pages"]), 7)
        self.assertEqual(
            [page["pdf_page"] for page in document["pages"]],
            list(range(1, 8)),
        )
        self.assertEqual(
            [page["page_index"] for page in document["pages"]],
            list(range(7)),
        )

    def test_a_deleted_page_is_gone_and_the_kept_pages_move_up(self):
        run, _ = self.read_run()

        mistral_ocr.finish_ready_applies()

        run.refresh_from_db()
        pages = self.objects[run.extract_key]["pages"]
        # The original's page 2 was deleted, so no page reads "volume 2".
        self.assertNotIn("volume 2", [page["md"] for page in pages])
        self.assertEqual(pages[0]["md"], "volume 1")
        self.assertEqual(pages[0]["source"]["kind"], "original")
        self.assertEqual(pages[0]["source"]["pdf_page"], 1)

    def test_an_edited_page_comes_from_its_own_read(self):
        run, (turn, swap, leaf) = self.read_run()

        mistral_ocr.finish_ready_applies()

        run.refresh_from_db()
        pages = self.objects[run.extract_key]["pages"]
        by_edit = {
            (page["source"].get("edit_id"), page["source"].get("page")): page
            for page in pages
            if page["source"]["kind"] == "edit"
        }
        self.assertEqual(
            by_edit[(leaf.pk, 0)]["md"], f"# edit {leaf.pk} page 0"
        )
        self.assertEqual(
            by_edit[(leaf.pk, 1)]["md"], f"# edit {leaf.pk} page 1"
        )
        self.assertEqual(
            by_edit[(swap.pk, 0)]["md"], f"# edit {swap.pk} page 0"
        )
        self.assertEqual(
            by_edit[(turn.pk, 0)]["md"], f"# edit {turn.pk} page 0"
        )
        # The shard's own numbering never reaches the document.
        self.assertNotIn("page_no", by_edit[(leaf.pk, 1)])
        self.assertNotIn("shard_index", by_edit[(leaf.pk, 1)])

    def test_an_identity_run_aliases_the_volume_document(self):
        self.edit(PageEdit.Kind.SET_NUMBER, pdf_page=2, value="12")
        run = apply.build_run(self.scan)
        volume_key = self.volume_extract_run()

        mistral_ocr.finish_ready_applies()

        run.refresh_from_db()
        self.assertEqual(run.extract_key, volume_key)
        self.assertEqual(run.extract_run, 1)

    def test_the_document_names_the_volume_run_it_read(self):
        run, _ = self.read_run()

        mistral_ocr.finish_ready_applies()

        run.refresh_from_db()
        self.assertEqual(run.extract_run, 1)
        self.assertEqual(self.objects[run.extract_key]["run"], 1)

    def test_the_rows_are_consumed_and_their_results_kept(self):
        run, _ = self.read_run()

        mistral_ocr.finish_ready_applies()

        rows = mistral_ocr.apply_jobs(self.scan, run)
        self.assertEqual({row.status for row in rows}, {JobStatus.CONSUMED})
        for row in rows:
            self.assertIn(row.result_key, self.objects)

    def test_the_pass_costs_three_queries_when_nothing_owes_anything(self):
        """Every volume ever read keeps a glued run for good, so the
        pre-check must not grow with the corpus (#245)."""
        run, _ = self.read_run()
        mistral_ocr.finish_ready_applies()

        with self.assertNumQueries(3):
            self.assertEqual(mistral_ocr.finish_ready_applies(), 0)

    def test_the_document_is_written_once(self):
        run, _ = self.read_run()

        self.assertEqual(mistral_ocr.finish_ready_applies(), 1)
        self.assertEqual(mistral_ocr.finish_ready_applies(), 0)

    def test_a_second_volume_read_makes_the_document_due_again(self):
        run, _ = self.read_run()
        mistral_ocr.finish_ready_applies()
        run.refresh_from_db()
        first = run.extract_key

        # A second read of the volume: a later run, glued.
        rows = mistral_ocr.ensure_extract_jobs(
            self.scan, make_manifest(1, self.PAGES), force_new_run=True
        )
        ExternalJob.objects.filter(pk__in=[r.pk for r in rows]).update(
            status=JobStatus.CONSUMED
        )
        key = mistral_ocr.glued_result_key(self.scan, rows[0].run)
        self.objects[key] = dict(
            self.objects[mistral_ocr.glued_result_key(self.scan, 1)],
            run=rows[0].run,
        )

        self.assertEqual(mistral_ocr.finish_ready_applies(), 1)

        run.refresh_from_db()
        self.assertEqual(run.extract_key, first)
        self.assertEqual(run.extract_run, rows[0].run)

    def test_a_missing_volume_page_is_a_failure_that_is_counted(self):
        run, _ = self.read_run()
        volume_key = mistral_ocr.glued_result_key(self.scan, 1)
        self.objects[volume_key] = dict(
            self.objects[volume_key],
            pages=self.objects[volume_key]["pages"][:1],
        )

        self.assertEqual(mistral_ocr.finish_ready_applies(), 0)

        run.refresh_from_db()
        self.assertEqual(run.extract_key, "")
        head = mistral_ocr.live_extract_jobs(self.scan)[0]
        self.assertEqual(
            head.provider_meta[f"glue:{run.label}"]["attempts"],
            1,
            "one ledger per corrected volume, beside the volume glue's",
        )
        self.assertNotIn("glue", head.provider_meta)

    def test_an_edit_with_no_row_keeps_its_slot_with_an_error(self):
        """A page the read never covered must not shift the pages after
        it: it keeps its slot and says so."""
        run, (_turn, _swap, leaf) = self.read_run()
        ExternalJob.objects.filter(
            apply_run=run,
            stage=JobStage.EXTRACT,
            input_manifest__edit_id=leaf.pk,
        ).delete()

        mistral_ocr.finish_ready_applies()

        run.refresh_from_db()
        pages = self.objects[run.extract_key]["pages"]
        holes = [page for page in pages if "error" in page]
        self.assertEqual(len(holes), 2)
        self.assertEqual(len(pages), 7)
        self.assertEqual(self.objects[run.extract_key]["failed_pages"], [4, 5])


class TestNoReviewStateWaits(MistralApplyTestCase):
    """The Mistral outputs gate nothing (#245)."""

    def test_a_complete_run_needs_no_extract_key(self):
        run, _ = self.built_run()
        ApplyRun.objects.filter(pk=run.pk).update(
            bitonal_key="b",
            ocr_key="o",
            printed_pages_key="p",
            detections_key="d",
        )
        run.refresh_from_db()

        self.assertEqual(run.extract_key, "")
        self.assertTrue(run.is_complete)

    def test_the_apply_owes_no_phase_for_the_mistral_document(self):
        """The corrected volume is finished without it: an arm in
        ``glues_due`` would wait for a read that starts later than the
        status the trigger takes."""
        run, _ = self.read_run()
        with patch("scanning.apply.glues_due", return_value=[]):
            self.assertIsNone(apply.phase_due(self.scan))

    def test_the_pass_writes_no_scan_status(self):
        run, _ = self.read_run()
        Status(self.scan.status)

        mistral_ocr.finish_ready_applies()

        before = self.scan.status
        self.scan.refresh_from_db()
        self.assertEqual(self.scan.status, before)
