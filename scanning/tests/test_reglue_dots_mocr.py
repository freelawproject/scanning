"""Tests for the ``reglue_dots_mocr`` command (issue #242).

The command is the backfill for the repair: it glues a run again over
the stored shard results, so a page whose layout JSON broke on one
character gets its cells back, and hands the volume to the apply. Under
test: which scans it takes, what the dry run reports and does not
write, and the refusals.
"""

import json
import shutil
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from scanning import dots_mocr, jobs
from scanning.factories import ScanFactory
from scanning.models import ExternalJob, JobStatus, Status
from scanning.tests.test_dots_mocr_glue import (
    make_envelope,
    make_filtered_page,
    make_page,
)
from scanning.tests.test_jobs import make_manifest


class TestReglueDotsMocr(TestCase):
    """One glue per volume with a filtered page, and nothing else."""

    def setUp(self):
        super().setUp()
        self.manifest = make_manifest(shard_count=2, pages_per_shard=2)
        # Keyed by the whole S3 key, so two scans in one test do not
        # answer each other's downloads.
        self.store: dict[str, dict] = {}
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

        def _download(key, dest_path):
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_text(json.dumps(self.store[key]))

        self.s3_active = self._patch(
            "scanning.s3_sync.s3_active", return_value=True
        )
        self.download = self._patch(
            "scanning.s3_sync.download_object", side_effect=_download
        )
        self.upload = self._patch(
            "scanning.s3_sync.upload_json_object", return_value=True
        )

    def _patch(self, target, **kwargs):
        """Start a patcher that stops with the test.

        :param target: The dotted path to patch.
        :param kwargs: Passed to ``mock.patch``.
        :returns: The mock.
        """
        patcher = patch(target, **kwargs)
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def _glued_scan(self, filtered=(0,), status=Status.AWAITING_VALIDATION):
        """Create a scan whose live run is glued and applied.

        :param filtered: Shard indexes whose page 1 is filtered.
        :param status: The scan's status.
        :returns: The scan.
        """
        scan = ScanFactory(status=status, page_count=4)
        rows = dots_mocr.ensure_analyze_jobs(scan, self.manifest)
        for row in rows:
            pages = [make_page(0), make_page(1)]
            if row.shard_index in filtered:
                pages[1] = make_filtered_page(1)
            key = (
                f"scans/{scan.pk}/jobs/analyze/dots_mocr"
                f"/r1-s{row.shard_index}-a1.json"
            )
            ExternalJob.objects.filter(pk=row.pk).update(
                status=JobStatus.CONSUMED,
                result_key=key,
                provider_meta={
                    "output": {
                        "page_count": 2,
                        "failed_pages": [],
                        "filtered_pages": (
                            [1] if row.shard_index in filtered else []
                        ),
                    }
                },
            )
            row.refresh_from_db()
            self.store[key] = make_envelope(row, pages)
        # The run applied, as every glued run in review 1 has.
        rows = dots_mocr.live_analyze_jobs(scan)
        dots_mocr._write_apply_state(
            rows, {"applied_at": timezone.now().isoformat()}
        )
        return scan

    def _call(self, *args):
        out, err = StringIO(), StringIO()
        call_command("reglue_dots_mocr", *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_a_volume_with_a_filtered_page_is_glued_again(self):
        scan = self._glued_scan(filtered=(0,))

        out, _ = self._call()

        document = self.upload.call_args[0][1]
        self.assertEqual(document["filtered_pages"], [])
        self.assertEqual(document["repaired_pages"], [1])
        self.assertIn("0 filtered page(s) left", out)
        self.assertIn("handed back to the apply", out)
        self.assertIn("1 scan(s) glued again", out)
        # The row no longer reports a hole, so no re-read is paid for.
        rows = dots_mocr.live_analyze_jobs(scan)
        self.assertFalse(jobs.has_unread_pages(rows[0]))
        # And the apply will run again over the new document.
        self.assertEqual(dots_mocr._apply_state(rows), {})

    def test_a_volume_with_no_filtered_page_is_skipped(self):
        scan = self._glued_scan(filtered=())

        out, err = self._call(str(scan.pk))

        self.upload.assert_not_called()
        self.assertIn("0 scan(s) glued again, 1 skipped", out)
        self.assertIn("no shard of the live run reports a filtered", err)

    def test_a_dry_run_reports_the_arm_and_writes_nothing(self):
        scan = self._glued_scan(filtered=(0, 1))

        out, _ = self._call("--dry-run")

        self.upload.assert_not_called()
        self.assertIn(
            f"scan {scan.pk} volume page 2 (shard 1 page 1): would repair "
            "by escape_quote@",
            out,
        )
        self.assertIn("volume page 4 (shard 2 page 1)", out)
        self.assertIn("2 filtered page(s) in 4 page(s)", out)
        self.assertIn("2 repairable (escape_quote 2), 0 not", out)
        self.assertIn("1 scan(s) surveyed", out)
        # Nothing moved: the run is still applied, the rows still say
        # they hold a hole.
        rows = dots_mocr.live_analyze_jobs(scan)
        self.assertTrue(dots_mocr._apply_state(rows).get("applied_at"))
        self.assertTrue(jobs.has_unread_pages(rows[0]))

    def test_a_dry_run_names_a_page_no_arm_reaches(self):
        scan = self._glued_scan(filtered=(0,))
        page = make_filtered_page(1)
        page["raw"] = page["raw"][:40]
        self._rewrite(scan, 0, [make_page(0), page])

        out, _ = self._call("--dry-run")

        self.assertIn("cannot repair: Unterminated string", out)
        self.assertIn("0 repairable, 1 not", out)

    def test_a_dry_run_names_a_page_with_no_stored_answer(self):
        scan = self._glued_scan(filtered=(0,))
        page = make_filtered_page(1)
        page.pop("raw")
        self._rewrite(scan, 0, [make_page(0), page])

        out, _ = self._call("--dry-run")

        self.assertIn("no answer stored", out)
        self.assertIn("1 with no stored answer", out)

    def test_a_dry_run_counts_what_the_threshold_rung_recovered(self):
        # Item 3 of #242: the share of filtered answers the retry rung
        # of #238 saves is what says whether it is worth 90s of GPU.
        scan = self._glued_scan(filtered=(0,))
        self._rewrite(
            scan, 0, [self._rung_recovered(0), make_filtered_page(1)]
        )

        out, _ = self._call("--dry-run")

        self.assertIn("recovered 1 filtered answer(s)", out)

    def test_a_dry_run_surveys_a_volume_with_no_filtered_page_left(self):
        # The bias this avoids: a volume whose retry rung recovered
        # every filtered answer carries no filtered page, so a survey
        # that skipped on the rows would never count the very evidence
        # that the rung works -- nor the volume's pages, which the rate
        # divides by.
        scan = self._glued_scan(filtered=())
        self._rewrite(scan, 0, [self._rung_recovered(0), make_page(1)])

        out, _ = self._call("--dry-run")

        self.assertIn("recovered 1 filtered answer(s)", out)
        self.assertIn("0 filtered page(s) in 4 page(s) of 1 volume(s)", out)
        self.assertIn("1 scan(s) surveyed", out)
        # Still a survey: nothing was written.
        self.upload.assert_not_called()
        rows = dots_mocr.live_analyze_jobs(scan)
        self.assertTrue(dots_mocr._apply_state(rows).get("applied_at"))

    def test_a_dry_run_reports_the_rate(self):
        # Item 1 of #242 asks for the true rate, so the summary names
        # the total and the one-in-N form, not only the counts.
        self._glued_scan(filtered=(0,))

        out, _ = self._call("--dry-run")

        self.assertIn(
            "1 filtered page(s) in 4 page(s) of 1 volume(s), about one in 4",
            out,
        )

    def test_the_writing_pass_still_skips_on_the_rows(self):
        # Only the survey reads every volume: the writing pass must not
        # download and re-upload a document for a volume with nothing
        # to repair.
        scan = self._glued_scan(filtered=())

        out, err = self._call(str(scan.pk))

        self.upload.assert_not_called()
        self.download.assert_not_called()
        self.assertIn("no shard of the live run reports a filtered", err)

    def _rung_recovered(self, page_no: int) -> dict:
        """Build a page the threshold rung of #238 saved from a filter.

        :param page_no: 0-based index inside the shard.
        :returns: A page dict.
        :rtype: dict
        """
        page = make_page(page_no)
        page.update(
            {
                "recovered_by": 2,
                "render": "threshold",
                "errors": ["model output was not layout JSON"],
            }
        )
        return page

    def test_an_approved_volume_is_left_alone(self):
        scan = self._glued_scan(
            filtered=(0,), status=Status.PAGE_COMPLETENESS_REVIEW_DONE
        )

        out, err = self._call(str(scan.pk))

        self.upload.assert_not_called()
        self.assertIn("0 scan(s) glued again", out)
        self.assertIn(f"scan {scan.pk}: no glued run in review 1", err)

    def test_a_ready_volume_is_glued_again(self):
        # A recompute: the apply keeps the status and the curator's own
        # numbers survive it.
        scan = self._glued_scan(
            filtered=(0,), status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )

        out, _ = self._call()

        self.assertIn("1 scan(s) glued again", out)
        scan.refresh_from_db()
        self.assertEqual(
            scan.status, Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )

    def test_a_run_not_glued_yet_is_skipped(self):
        scan = self._glued_scan(filtered=(0,))
        ExternalJob.objects.filter(scan=scan, shard_index=1).update(
            status=JobStatus.COMPLETED
        )

        out, err = self._call(str(scan.pk))

        self.upload.assert_not_called()
        self.assertIn("the live dots.mocr run is not glued yet", err)

    def test_a_glue_failure_names_the_scan_and_the_pass_goes_on(self):
        first = self._glued_scan(filtered=(0,))
        second = self._glued_scan(filtered=(0,))
        # A worker deployed ahead of the daemon: every envelope is
        # refused, and both volumes have to be named.
        for key in list(self.store):
            self.store[key] = {"schema_version": 9}

        out, err = self._call(str(first.pk), str(second.pk))

        self.assertIn(f"scan {first.pk}:", err)
        self.assertIn(f"scan {second.pk}:", err)
        self.assertIn("0 scan(s) glued again, 2 skipped", out)

    def test_named_scans_only(self):
        first = self._glued_scan(filtered=(0,))
        self._glued_scan(filtered=(0,))

        out, _ = self._call(str(first.pk))

        self.assertIn("1 scan(s) glued again", out)

    def test_without_s3_the_command_refuses(self):
        self.s3_active.return_value = False

        with self.assertRaises(CommandError) as caught:
            self._call()

        self.assertIn("S3 is not active", str(caught.exception))

    def _rewrite(self, scan, shard_index: int, pages: list[dict]) -> None:
        """Replace one shard's stored result.

        :param scan: The scan the shard belongs to.
        :param shard_index: Which shard.
        :param pages: The page dicts to store.
        :return: None.
        """
        row = dots_mocr.live_analyze_jobs(scan)[shard_index]
        self.store[row.result_key] = make_envelope(row, pages)
