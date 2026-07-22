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

    def test_requeue_scans_skips_non_error_and_reports(self):
        err = ScanFactory(status=Status.ERROR)
        done = ScanFactory(status=Status.PENDING_REVIEW)
        msgs = self._run(
            "requeue_scans",
            Scan.objects.filter(pk__in=[err.pk, done.pk]),
        )
        err.refresh_from_db()
        done.refresh_from_db()
        self.assertEqual(err.status, Status.QUEUED)
        self.assertEqual(done.status, Status.PENDING_REVIEW)
        self.assertIn("Skipped 1", msgs[0].message)

    def test_requeue_scans_warns_when_nothing_matched(self):
        scan = ScanFactory(status=Status.PENDING_REVIEW)
        msgs = self._run("requeue_scans", Scan.objects.filter(pk=scan.pk))
        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.PENDING_REVIEW)
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
