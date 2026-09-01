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
"""

from unittest.mock import patch

from botocore.exceptions import ClientError
from django.contrib import messages
from django.contrib.admin.sites import AdminSite
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory, TestCase

from scanning.admin import ScanAdmin
from scanning.factories import ExternalJobFactory, ScanFactory, UserFactory
from scanning.models import JobStage, PendingUpload, Scan, Status


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

    def test_requeue_scans_covers_all_error_and_busy_statuses(self):
        scans = [
            ScanFactory(status=Status.ERROR),
            ScanFactory(status=Status.ERROR_MAX_RETRIES),
            ScanFactory(status=Status.ERROR_INTERRUPTED),
            ScanFactory(status=Status.PROCESSING),
            # A scan stuck behind external jobs that will never finish
            # has to be recoverable too (issue #176).
            ScanFactory(status=Status.AWAITING),
        ]
        pks = [s.pk for s in scans]
        self._run("requeue_scans", Scan.objects.filter(pk__in=pks))
        self.assertEqual(
            Scan.objects.filter(pk__in=pks, status=Status.QUEUED).count(), 5
        )

    def test_requeue_cancels_the_scans_open_jobs(self):
        """A re-queue means start over, so nothing may still be in flight.

        Leaving a row open would let a stale attempt's outcome land on a
        scan that has moved on, and would make ``ensure_convert_jobs``
        reuse rows whose result objects another attempt is still writing.
        """
        from scanning.models import JobStatus

        scan = ScanFactory(status=Status.AWAITING)
        in_flight = ExternalJobFactory(
            scan=scan, stage=JobStage.CONVERT, status=JobStatus.SUBMITTED
        )
        other = ScanFactory(status=Status.PENDING_REVIEW)
        untouched = ExternalJobFactory(
            scan=other, stage=JobStage.CONVERT, status=JobStatus.SUBMITTED
        )

        self._run("requeue_scans", Scan.objects.filter(pk=scan.pk))

        in_flight.refresh_from_db()
        untouched.refresh_from_db()
        self.assertEqual(in_flight.status, JobStatus.CANCELLED)
        self.assertEqual(in_flight.error_code, "ABANDONED")
        self.assertEqual(untouched.status, JobStatus.SUBMITTED)

    def test_requeue_leaves_a_skipped_scans_jobs_alone(self):
        """Only the scans the action actually re-queues are abandoned."""
        from scanning.models import JobStatus

        scan = ScanFactory(status=Status.PENDING_REVIEW)
        job = ExternalJobFactory(
            scan=scan, stage=JobStage.CONVERT, status=JobStatus.SUBMITTED
        )

        self._run("requeue_scans", Scan.objects.filter(pk=scan.pk))

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.SUBMITTED)

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


class ScanAdminDeleteShardSweepTests(TestCase):
    """Scan deletion sweeps the shards, job results and page edits.

    The three prefixes nothing else removes (PR #169 review, issues
    #176 and #214). The shard PDFs duplicate the original's bytes, each
    job result holds a converted copy of the volume, and the page edit
    images are the only copy of what a curator uploaded.
    """

    def setUp(self):
        self.admin = ScanAdmin(Scan, AdminSite())

    def test_delete_model_sweeps_shards_and_job_results(self):
        scan = ScanFactory()
        with (
            patch("scanning.s3_sync.delete_shard_objects") as shards,
            patch("scanning.s3_sync.delete_job_objects") as results,
            patch("scanning.s3_sync.delete_page_edit_objects") as images,
        ):
            self.admin.delete_model(_request_with_messages(), scan)
        for mock_delete in (shards, results, images):
            mock_delete.assert_called_once()
            self.assertEqual(mock_delete.call_args.args[0].pk, scan.pk)
        self.assertFalse(Scan.objects.filter(pk=scan.pk).exists())

    def test_delete_queryset_sweeps_each_scan(self):
        scans = [ScanFactory(), ScanFactory()]
        queryset = Scan.objects.filter(pk__in=[s.pk for s in scans])
        with patch("scanning.s3_sync.delete_shard_objects") as mock_delete:
            with patch("scanning.s3_sync.delete_job_objects") as results:
                self.admin.delete_queryset(_request_with_messages(), queryset)
        self.assertEqual(mock_delete.call_count, 2)
        self.assertEqual(results.call_count, 2)
        self.assertEqual(
            sorted(c.args[0].pk for c in mock_delete.call_args_list),
            sorted(s.pk for s in scans),
        )
        self.assertFalse(queryset.exists())

    def test_delete_survives_an_s3_failure(self):
        """Best effort, and one prefix failing must not skip the other."""
        scan = ScanFactory()
        error = ClientError({"Error": {"Code": "SlowDown"}}, "DeleteObjects")
        with (
            patch("scanning.s3_sync.delete_shard_objects", side_effect=error),
            patch("scanning.s3_sync.delete_job_objects") as results,
            patch("scanning.s3_sync.delete_page_edit_objects") as images,
        ):
            self.admin.delete_model(_request_with_messages(), scan)
        results.assert_called_once()
        images.assert_called_once()
        self.assertFalse(Scan.objects.filter(pk=scan.pk).exists())


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
