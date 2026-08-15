"""Tests for ``scanning.admin`` re-queue actions.

The ``ScanAdmin`` re-queue actions are called directly on the admin
instance with a ``RequestFactory`` request wired for the messages
framework; nothing goes through the admin HTTP views.

Covers:

- ``requeue_scans`` (general recovery): re-queues any error / stuck
  Processing scan, resets both counters, skips and reports non-error
  selections.
- ``requeue_retry_cap_scans`` / ``requeue_interrupted_scans``: narrow
  status-scoped variants that warn (rather than silently succeed) when
  the selection contains no matching scan.
- ``get_deleted_objects``: the hand-rolled blast-radius summary, which
  has to name every cascade table explicitly.
- What a re-queue does to the scan's jobs, which is what decides
  whether the action means anything at all in batch mode.
"""

from unittest.mock import MagicMock, patch

from django.contrib import messages
from django.contrib.admin.sites import AdminSite
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory, TestCase

from scanning.admin import ScanAdmin
from scanning.factories import ExternalJobFactory, ScanFactory, UserFactory
from scanning.models import (
    JobStage,
    JobStatus,
    PendingUpload,
    Scan,
    Status,
)


def _request_with_messages():
    """Build a POST request wired for ``ModelAdmin.message_user``."""
    request = RequestFactory().post("/admin/scanning/scan/")
    SessionMiddleware(lambda r: None).process_request(request)
    request.session.save()
    request._messages = FallbackStorage(request)
    return request


class ScanAdminRequeueActionTests(TestCase):
    """``ScanAdmin`` re-queue actions."""

    def setUp(self):
        self.admin = ScanAdmin(Scan, AdminSite())

    def _run(self, action_name, queryset):
        """Invoke an action and return the captured messages list."""
        request = _request_with_messages()
        getattr(self.admin, action_name)(request, queryset)
        return list(request._messages)

    # ── requeue_scans (general) ──────────────────────────────────────
    def test_requeue_scans_requeues_plain_error(self):
        scan = ScanFactory(status=Status.ERROR)
        msgs = self._run("requeue_scans", Scan.objects.filter(pk=scan.pk))
        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.QUEUED)
        self.assertEqual(msgs[0].level, messages.SUCCESS)

    def test_requeue_scans_resets_both_counters(self):
        scan = ScanFactory(
            status=Status.ERROR_MAX_RETRIES,
            retry_count=5,
            interruption_count=3,
        )
        self._run("requeue_scans", Scan.objects.filter(pk=scan.pk))
        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.QUEUED)
        self.assertEqual(scan.retry_count, 0)
        self.assertEqual(scan.interruption_count, 0)

    def test_requeue_scans_covers_all_error_and_processing(self):
        scans = [
            ScanFactory(status=Status.ERROR),
            ScanFactory(status=Status.ERROR_MAX_RETRIES),
            ScanFactory(status=Status.ERROR_INTERRUPTED),
            ScanFactory(status=Status.PROCESSING),
        ]
        pks = [s.pk for s in scans]
        self._run("requeue_scans", Scan.objects.filter(pk__in=pks))
        self.assertEqual(
            Scan.objects.filter(pk__in=pks, status=Status.QUEUED).count(), 4
        )

    def test_requeue_scans_skips_non_error_leaves_it_untouched(self):
        err = ScanFactory(status=Status.ERROR)
        done = ScanFactory(
            status=Status.PENDING_REVIEW,
            retry_count=2,
            interruption_count=1,
        )
        msgs = self._run(
            "requeue_scans",
            Scan.objects.filter(pk__in=[err.pk, done.pk]),
        )
        err.refresh_from_db()
        done.refresh_from_db()
        self.assertEqual(err.status, Status.QUEUED)
        # The skipped scan keeps its status AND its counters.
        self.assertEqual(done.status, Status.PENDING_REVIEW)
        self.assertEqual(done.retry_count, 2)
        self.assertEqual(done.interruption_count, 1)
        # A partial run warns and reports the skip.
        self.assertEqual(msgs[0].level, messages.WARNING)
        self.assertIn("Skipped 1", msgs[0].message)

    def test_requeue_scans_resets_progress_fields(self):
        scan = ScanFactory(
            status=Status.ERROR,
            progress_message="frozen mid-OCR",
            progress_current=137,
            progress_total=943,
        )
        self._run("requeue_scans", Scan.objects.filter(pk=scan.pk))
        scan.refresh_from_db()
        self.assertEqual(scan.progress_message, "Re-queued via admin")
        self.assertEqual(scan.progress_current, 0)
        self.assertEqual(scan.progress_total, 0)

    def test_requeue_scans_warns_when_nothing_matched(self):
        scan = ScanFactory(status=Status.PENDING_REVIEW)
        msgs = self._run("requeue_scans", Scan.objects.filter(pk=scan.pk))
        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.PENDING_REVIEW)
        self.assertEqual(msgs[0].level, messages.WARNING)
        self.assertIn("nothing re-queued", msgs[0].message)

    def test_requeue_scans_empty_selection_warns(self):
        msgs = self._run("requeue_scans", Scan.objects.none())
        self.assertEqual(msgs[0].level, messages.WARNING)

    # ── requeue_retry_cap_scans (scoped) ─────────────────────────────
    def test_retry_cap_action_requeues_matching(self):
        scan = ScanFactory(status=Status.ERROR_MAX_RETRIES, retry_count=5)
        msgs = self._run(
            "requeue_retry_cap_scans", Scan.objects.filter(pk=scan.pk)
        )
        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.QUEUED)
        self.assertEqual(scan.retry_count, 0)
        self.assertEqual(msgs[0].level, messages.SUCCESS)

    def test_retry_cap_action_leaves_non_matching_untouched(self):
        cap = ScanFactory(status=Status.ERROR_MAX_RETRIES, retry_count=5)
        plain = ScanFactory(status=Status.ERROR, retry_count=1)
        msgs = self._run(
            "requeue_retry_cap_scans",
            Scan.objects.filter(pk__in=[cap.pk, plain.pk]),
        )
        cap.refresh_from_db()
        plain.refresh_from_db()
        # Only the retry-cap scan is touched; the plain Error is left as-is.
        self.assertEqual(cap.status, Status.QUEUED)
        self.assertEqual(plain.status, Status.ERROR)
        self.assertEqual(plain.retry_count, 1)
        self.assertEqual(msgs[0].level, messages.WARNING)
        self.assertIn("Left 1", msgs[0].message)

    def test_retry_cap_action_warns_on_plain_error(self):
        scan = ScanFactory(status=Status.ERROR)
        msgs = self._run(
            "requeue_retry_cap_scans", Scan.objects.filter(pk=scan.pk)
        )
        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.ERROR)
        self.assertEqual(msgs[0].level, messages.WARNING)

    # ── requeue_interrupted_scans (scoped) ───────────────────────────
    def test_interrupted_action_requeues_matching(self):
        scan = ScanFactory(
            status=Status.ERROR_INTERRUPTED, interruption_count=4
        )
        msgs = self._run(
            "requeue_interrupted_scans", Scan.objects.filter(pk=scan.pk)
        )
        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.QUEUED)
        self.assertEqual(scan.interruption_count, 0)
        self.assertEqual(msgs[0].level, messages.SUCCESS)

    def test_interrupted_action_warns_on_plain_error(self):
        scan = ScanFactory(status=Status.ERROR)
        msgs = self._run(
            "requeue_interrupted_scans", Scan.objects.filter(pk=scan.pk)
        )
        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.ERROR)
        self.assertEqual(msgs[0].level, messages.WARNING)


class ScanAdminRequeueJobTests(TestCase):
    """What a re-queue does to the scan's ``ExternalJob`` rows.

    A status flip on its own is not a re-queue: ``ensure_jobs`` reuses a
    stage's live run whenever it still describes the same input, so
    rows left behind are found again on the next pass and park or fail
    the scan on the very work the operator was trying to get past.
    """

    def setUp(self):
        self.admin = ScanAdmin(Scan, AdminSite())
        self.provider = MagicMock()
        patcher = patch(
            "scanning.jobs.get_provider", return_value=self.provider
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _requeue(self, scan):
        """Run the general re-queue action over one scan."""
        request = _request_with_messages()
        self.admin.requeue_scans(request, Scan.objects.filter(pk=scan.pk))
        scan.refresh_from_db()

    def test_requeue_drops_a_dead_job(self):
        scan = ScanFactory(status=Status.ERROR)
        ExternalJobFactory(scan=scan, status=JobStatus.FAILED)

        self._requeue(scan)

        self.assertEqual(scan.status, Status.QUEUED)
        self.assertEqual(scan.jobs.count(), 0)

    def test_requeue_from_awaiting_cancels_and_drops_a_live_job(self):
        # The state whose whole meaning is "waiting on a job": the
        # operator re-queueing out of it is saying the job is not
        # coming back.
        scan = ScanFactory(status=Status.AWAITING)
        ExternalJobFactory(
            scan=scan, status=JobStatus.IN_PROGRESS, external_id="job-xyz"
        )

        self._requeue(scan)

        self.assertEqual(scan.status, Status.QUEUED)
        self.assertEqual(scan.jobs.count(), 0)
        self.assertEqual(self.provider.cancel.call_count, 1)

    def test_requeue_from_awaiting_keeps_a_finished_job(self):
        # Its result is on S3 and already paid for, so the next pass
        # consumes it instead of re-running the stage.
        scan = ScanFactory(status=Status.AWAITING)
        job = ExternalJobFactory(scan=scan, status=JobStatus.COMPLETED)

        self._requeue(scan)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.COMPLETED)
        self.provider.cancel.assert_not_called()

    def test_requeue_from_processing_keeps_a_live_job(self):
        # Recovery from a killed daemon, not from a wedged job. The
        # work may still be running, and cancelling it would pay for
        # the same GPU time twice.
        scan = ScanFactory(status=Status.PROCESSING)
        job = ExternalJobFactory(
            scan=scan, status=JobStatus.IN_PROGRESS, external_id="job-xyz"
        )

        self._requeue(scan)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.IN_PROGRESS)
        self.provider.cancel.assert_not_called()

    def test_requeue_keeps_a_consumed_stage(self):
        # What makes a re-queue cheap: it resumes at the step that
        # broke rather than at the top.
        scan = ScanFactory(status=Status.ERROR)
        done = ExternalJobFactory(
            scan=scan, stage=JobStage.DETECT, status=JobStatus.CONSUMED
        )
        ExternalJobFactory(
            scan=scan, stage=JobStage.ANALYZE, status=JobStatus.FAILED
        )

        self._requeue(scan)

        self.assertEqual([j.pk for j in scan.jobs.all()], [done.pk])


class ScanAdminDeleteSummaryTests(TestCase):
    """``ScanAdmin.get_deleted_objects``.

    The default collector is replaced so a processed scan's cascade does
    not exhaust memory, and the cost of that is losing Django's
    automatic discovery: a cascade table missing from the hand-written
    list is deleted without the operator being told.
    """

    def setUp(self):
        self.admin = ScanAdmin(Scan, AdminSite())

    def test_summary_counts_external_jobs(self):
        scan = ScanFactory()
        ExternalJobFactory(scan=scan, stage=JobStage.DETECT)
        ExternalJobFactory(scan=scan, stage=JobStage.ANALYZE)

        _, model_count, _, _ = self.admin.get_deleted_objects(
            [scan], _request_with_messages()
        )

        self.assertEqual(model_count.get("external jobs"), 2)

    def test_summary_counts_pending_uploads(self):
        """A pre-existing cascade that the summary used to leave out."""
        scan = ScanFactory()
        PendingUpload.objects.create(
            scan=scan,
            s3_key=f"processing/{scan.pk}/x/1/1.original.pdf",
            expected_size=1024,
            created_by=UserFactory(),
        )

        _, model_count, _, _ = self.admin.get_deleted_objects(
            [scan], _request_with_messages()
        )

        self.assertEqual(model_count.get("pending uploads"), 1)

    def test_summary_omits_tables_with_nothing_to_delete(self):
        scan = ScanFactory()

        _, model_count, _, _ = self.admin.get_deleted_objects(
            [scan], _request_with_messages()
        )

        self.assertNotIn("external jobs", model_count)
        self.assertNotIn("pending uploads", model_count)
