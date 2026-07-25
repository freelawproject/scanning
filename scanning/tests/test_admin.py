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
"""

from django.contrib import messages
from django.contrib.admin.sites import AdminSite
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory, TestCase

from scanning.admin import ScanAdmin
from scanning.factories import ScanFactory
from scanning.models import Scan, Status


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
