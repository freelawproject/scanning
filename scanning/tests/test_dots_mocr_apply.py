"""Tests for the dots.mocr apply pass (issues #149/#204, #212).

``apply_ready_runs`` reads each glued volume JSON, rebuilds
``ocr_results`` and the Issues, and takes the scan over the review edge
with one compare-and-swap. Under test:

- the edge: which statuses take it, which defer, and that the pass
  never transits QUEUED/PROCESSING
- the run-scoped ``applied_at`` marker that keeps a recompute from
  looping every tick
- the bounded retry of an apply that keeps failing, mirroring the glue
"""

import pathlib
from unittest.mock import patch

from django.test import TestCase

from scanning import dots_mocr
from scanning.factories import ScanFactory
from scanning.models import (
    CheckName,
    ExternalJob,
    JobStatus,
    Scan,
    Status,
)
from scanning.tests.test_jobs import make_manifest
from scanning.tests.test_page_numbers import cell, make_page


def make_document(texts) -> dict:
    """Build a glued volume document with one header cell per page.

    :param texts: One header text per page; None makes the page a
        filtered one.
    :returns: The document dict.
    :rtype: dict
    """
    return {
        "pages": [
            make_page(index + 1, None if text is None else [cell(text)])
            for index, text in enumerate(texts)
        ]
    }


class ApplyRunsMixin:
    """Builds a scan whose dots.mocr run is glued (all rows CONSUMED)."""

    def build(self, status=Status.AWAITING_VALIDATION, texts=("1", "2")):
        """Create a scan, a consumed run, and a patched glued document.

        :param status: The scan's status.
        :param texts: Header texts for the patched volume document.
        :returns: ``(scan, rows)``.
        """
        scan = ScanFactory(
            status=status, start_page=1, end_page=2, page_count=2
        )
        pathlib.Path(scan.original_pdf.path).unlink()
        rows = dots_mocr.ensure_analyze_jobs(scan, make_manifest(2, 1))
        ExternalJob.objects.filter(pk__in=[row.pk for row in rows]).update(
            status=JobStatus.CONSUMED
        )

        active = patch("scanning.s3_sync.s3_active", return_value=True)
        active.start()
        self.addCleanup(active.stop)
        download = patch(
            "scanning.s3_sync.download_json_object",
            return_value=make_document(texts),
        )
        self.download = download.start()
        self.addCleanup(download.stop)
        return scan, dots_mocr.live_analyze_jobs(scan)


class TestApplyReadyRuns(ApplyRunsMixin, TestCase):
    """The apply pass and its review edge."""

    def test_a_parked_scan_takes_the_edge(self):
        scan, rows = self.build()

        applied = dots_mocr.apply_ready_runs()

        scan.refresh_from_db()
        self.assertEqual(applied, 1)
        self.assertEqual(
            scan.status, Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )
        self.assertEqual(
            [(r["pdf_page"], r["detected"]) for r in scan.ocr_results],
            [(1, "1"), (2, "2")],
        )
        self.assertTrue(scan.page_map)
        fresh = dots_mocr.live_analyze_jobs(scan)
        self.assertTrue(dots_mocr._apply_state(fresh)["applied_at"])
        self.download.assert_called_once_with(
            dots_mocr.glued_result_key(scan, rows[0].run)
        )

    def test_a_legacy_scan_takes_the_edge_too(self):
        scan, _ = self.build(status=Status.PENDING_REVIEW)

        dots_mocr.apply_ready_runs()

        scan.refresh_from_db()
        self.assertEqual(
            scan.status, Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )

    def test_a_missing_page_number_becomes_an_issue(self):
        scan, _ = self.build(texts=("1", None))

        dots_mocr.apply_ready_runs()

        self.assertTrue(
            scan.issues.filter(
                check_name=CheckName.NO_PAGE_NUMBER, page_number=2
            ).exists()
        )

    def test_a_recompute_keeps_ready(self):
        scan, _ = self.build(status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW)

        applied = dots_mocr.apply_ready_runs()

        scan.refresh_from_db()
        self.assertEqual(applied, 1)
        self.assertEqual(
            scan.status, Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )
        self.assertEqual(scan.ocr_results[0]["detected"], "1")

    def test_an_applied_run_is_not_applied_again(self):
        """``applied_at`` is the idempotence marker, scoped to the run."""
        self.build()

        dots_mocr.apply_ready_runs()
        applied = dots_mocr.apply_ready_runs()

        self.assertEqual(applied, 0)
        self.download.assert_called_once()

    def test_the_pass_never_queues_daemon_work(self):
        """The point of #212: the apply runs in place, so the scan
        stays in the review flow and no queued action is left behind."""
        scan, _ = self.build()

        dots_mocr.apply_ready_runs()

        scan.refresh_from_db()
        self.assertEqual(
            scan.status, Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )
        self.assertEqual(scan.queued_action, "")

    def test_an_awaiting_scan_is_deferred_without_cost(self):
        """No attempt is spent: the tick after the bitonal park applies."""
        scan, rows = self.build(status=Status.AWAITING)

        applied = dots_mocr.apply_ready_runs()

        scan.refresh_from_db()
        self.assertEqual(applied, 0)
        self.assertEqual(scan.status, Status.AWAITING)
        self.assertEqual(scan.ocr_results, [])
        self.assertEqual(dots_mocr._apply_state(rows), {})

        Scan.objects.filter(pk=scan.pk).update(
            status=Status.AWAITING_VALIDATION
        )
        self.assertEqual(dots_mocr.apply_ready_runs(), 1)

    def test_terminal_scans_are_never_revived(self):
        for status in (Status.CANCELLED, Status.ERROR, Status.APPROVED):
            with self.subTest(status=status):
                scan, _ = self.build(status=status)

                dots_mocr.apply_ready_runs()

                scan.refresh_from_db()
                self.assertEqual(scan.status, status)
                self.assertEqual(scan.ocr_results, [])

    def test_an_unglued_run_is_left_to_the_glue(self):
        scan, rows = self.build()
        ExternalJob.objects.filter(pk=rows[0].pk).update(
            status=JobStatus.COMPLETED
        )

        applied = dots_mocr.apply_ready_runs()

        scan.refresh_from_db()
        self.assertEqual(applied, 0)
        self.assertEqual(scan.status, Status.AWAITING_VALIDATION)

    def test_the_tries_run_out_loudly_then_quietly(self):
        """Mirror of the glue's bounded retry."""
        scan, _ = self.build()
        self.download.side_effect = RuntimeError("boom")

        for attempt in range(1, dots_mocr.APPLY_MAX_ATTEMPTS + 1):
            if attempt < dots_mocr.APPLY_MAX_ATTEMPTS:
                with self.assertLogs("scanning.dots_mocr", "WARNING") as logs:
                    dots_mocr.apply_ready_runs()
                self.assertTrue(any("attempt" in line for line in logs.output))
            else:
                with self.assertLogs("scanning.dots_mocr", "ERROR") as logs:
                    dots_mocr.apply_ready_runs()
                self.assertTrue(
                    any("giving up" in line for line in logs.output)
                )

        state = dots_mocr._apply_state(dots_mocr.live_analyze_jobs(scan))
        self.assertEqual(state["attempts"], dots_mocr.APPLY_MAX_ATTEMPTS)
        self.assertIn("boom", state["last_error"])

        # Out of tries: the pass skips the run silently.
        calls = self.download.call_count
        dots_mocr.apply_ready_runs()
        self.assertEqual(self.download.call_count, calls)
        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.AWAITING_VALIDATION)

    def test_a_failure_then_a_fix_recovers_next_tick(self):
        scan, _ = self.build()
        self.download.side_effect = RuntimeError("blip")

        dots_mocr.apply_ready_runs()
        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.AWAITING_VALIDATION)

        self.download.side_effect = None
        applied = dots_mocr.apply_ready_runs()

        scan.refresh_from_db()
        self.assertEqual(applied, 1)
        self.assertEqual(
            scan.status, Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )
