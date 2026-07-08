"""Tests for web-worker observability (issue #115).

These exercise the pure tick logic and the middleware registry directly, without
starting real background threads.
"""

from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, SimpleTestCase

from scanning import observability
from scanning.middleware import InFlightRequestMiddleware
from scanning.observability import InFlightRequest, WebMonitor


class RegistryTestMixin:
    """Reset the shared in-flight registry around each test."""

    def setUp(self) -> None:
        super().setUp()
        observability._registry.clear()
        self.addCleanup(observability._registry.clear)


class InFlightMiddlewareTest(RegistryTestMixin, SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.factory = RequestFactory()

    def test_entry_present_during_request_and_cleared_after(self) -> None:
        seen: dict[int, InFlightRequest] = {}

        def get_response(request):
            # The registry is populated while the view runs.
            seen.update(observability._registry)
            from django.http import HttpResponse

            return HttpResponse("ok")

        middleware = InFlightRequestMiddleware(get_response)
        # Query string included: register_request uses get_full_path().
        request = self.factory.get("/scan/42/?page=3&dpi=300")
        middleware(request)

        self.assertEqual(len(seen), 1)
        (entry,) = seen.values()
        self.assertEqual(entry.path, "/scan/42/?page=3&dpi=300")
        self.assertEqual(entry.method, "GET")
        # Cleared on completion.
        self.assertEqual(observability._registry, {})

    def test_entry_cleared_even_when_view_raises(self) -> None:
        def get_response(request):
            raise ValueError("boom")

        middleware = InFlightRequestMiddleware(get_response)
        with self.assertRaises(ValueError):
            middleware(self.factory.get("/scan/42/"))
        # The finally in __call__ must still clear the entry.
        self.assertEqual(observability._registry, {})

    def test_process_view_records_scan_pk_and_user(self) -> None:
        middleware = InFlightRequestMiddleware(lambda r: None)
        request = self.factory.get("/scan/42/")
        request.user = AnonymousUser()

        observability.register_request(1234, "/scan/42/", "GET")
        with mock.patch("threading.get_ident", return_value=1234):
            middleware.process_view(request, lambda r: None, (), {"pk": 42})

        entry = observability._registry[1234]
        self.assertEqual(entry.scan_pk, "42")
        self.assertEqual(entry.user, "anonymous")


class WebMonitorTest(RegistryTestMixin, SimpleTestCase):
    def _monitor(self, **kwargs) -> WebMonitor:
        defaults = dict(interval=10.0, inflight_warn=30.0)
        defaults.update(kwargs)
        return WebMonitor(**defaults)

    def test_concurrency_line_logged(self) -> None:
        observability._registry[1] = InFlightRequest(
            path="/scan/7/", method="GET", start=99.0, scan_pk="7", user="a"
        )
        monitor = self._monitor()
        with self.assertLogs("scanning.observability", level="INFO") as logs:
            monitor.run_checks(now=100.0)
        self.assertIn("web-monitor inflight=1", "\n".join(logs.output))

    def test_hung_request_is_logged(self) -> None:
        observability._registry[1] = InFlightRequest(
            path="/scan/7/process/",
            method="POST",
            start=0.0,
            scan_pk="7",
            user="alice",
        )
        monitor = self._monitor(inflight_warn=30.0)
        with self.assertLogs(
            "scanning.observability", level="WARNING"
        ) as logs:
            monitor.run_checks(now=100.0)  # elapsed 100s >= 30s

        joined = "\n".join(logs.output)
        self.assertIn("hung request", joined)
        self.assertIn("scan_pk=7", joined)
        self.assertIn("/scan/7/process/", joined)

    def test_fresh_request_not_flagged(self) -> None:
        observability._registry[1] = InFlightRequest(
            path="/scan/7/",
            method="GET",
            start=95.0,
            scan_pk="7",
            user="alice",
        )
        monitor = self._monitor(inflight_warn=30.0)
        with self.assertLogs("scanning.observability", level="INFO") as logs:
            monitor.run_checks(now=100.0)  # elapsed 5s < 30s
        self.assertNotIn("hung request", "\n".join(logs.output))


class SnapshotForReportTest(RegistryTestMixin, SimpleTestCase):
    """The snapshot attached to the WORKER TIMEOUT Sentry event (issue #115)."""

    def test_empty_registry_returns_empty_list(self) -> None:
        self.assertEqual(observability.snapshot_for_report(now=100.0), [])

    def test_computes_elapsed_and_orders_longest_first(self) -> None:
        observability._registry[1] = InFlightRequest(
            path="/scans/7/original-crop/?page=15&dpi=300",
            method="GET",
            start=40.0,  # elapsed 60s
            scan_pk="7",
            user="alice",
        )
        observability._registry[2] = InFlightRequest(
            path="/scans/9/process/",
            method="POST",
            start=95.0,  # elapsed 5s
            scan_pk="9",
            user="bob",
        )

        snapshot = observability.snapshot_for_report(now=100.0)

        # Longest-running request first, with a JSON-serializable payload.
        self.assertEqual([item["scan_pk"] for item in snapshot], ["7", "9"])
        self.assertEqual(snapshot[0]["elapsed_s"], 60.0)
        self.assertEqual(snapshot[0]["method"], "GET")
        self.assertEqual(
            snapshot[0]["path"], "/scans/7/original-crop/?page=15&dpi=300"
        )
        self.assertEqual(snapshot[0]["user"], "alice")
        self.assertEqual(snapshot[1]["elapsed_s"], 5.0)
