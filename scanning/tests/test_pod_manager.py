"""Tests for scanning.pod_manager and the pod-transport runpod_client."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from scanning import pod_manager, runpod_client
from scanning.factories import ScanFactory
from scanning.management.commands.stop_idle_gpu_pod import (
    Command as StopIdleCommand,
)
from scanning.models import Status


def _mock_response(status_code: int, body: dict):
    """Build a stand-in for a requests.Response.

    :param status_code: HTTP status.
    :param body: Parsed JSON body.
    :return: MagicMock that behaves like a Response for the ways
        runpod_client and pod_manager use one.
    :rtype: MagicMock
    """
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = body
    r.text = str(body)
    return r


@override_settings(
    RUNPOD_API_KEY="secret",
    RUNPOD_POD_ID="pod-abc",
    RUNPOD_POD_API_KEY="bearer",
    RUNPOD_POD_PORT=8000,
    RUNPOD_POD_BOOT_TIMEOUT=5,
)
class TestEnsurePodReady(TestCase):
    """pod_manager.ensure_pod_ready lifecycle."""

    def setUp(self):
        cache.clear()

    def test_already_running_fast_path_returns_zero_boot(self):
        """If /pods/{id} says RUNNING and /health returns 200,
        ensure_pod_ready returns the proxy URL and boot_ms=0."""
        pod_running = {
            "id": "pod-abc",
            "desiredStatus": "RUNNING",
            "publicIp": "1.2.3.4",
            "portMappings": {"8000": 10023},
        }
        with (
            patch(
                "scanning.pod_manager.requests.get",
                return_value=_mock_response(200, pod_running),
            ) as mock_get,
            patch(
                "scanning.pod_manager.requests.post",
            ) as mock_post,
        ):
            base_url, boot_ms = pod_manager.ensure_pod_ready()

        self.assertEqual(base_url, "https://pod-abc-8000.proxy.runpod.net")
        self.assertEqual(boot_ms, 0)
        # Fast path makes one GET (status) + one GET (health). No POST to start.
        self.assertEqual(mock_get.call_count, 2)
        mock_post.assert_not_called()

    def test_starts_pod_and_polls_until_ready(self):
        """Starts the pod if EXITED and polls /pods/{id} + /health."""
        pod_exited = {"id": "pod-abc", "desiredStatus": "EXITED"}
        pod_ready = {
            "id": "pod-abc",
            "desiredStatus": "RUNNING",
            "publicIp": "1.2.3.4",
            "portMappings": {"8000": 10023},
        }
        # get_pod_status responses: first EXITED, then RUNNING.
        # _probe_health: returns 200 once.
        status_sequence = [
            _mock_response(200, pod_exited),
            _mock_response(200, pod_ready),
        ]
        health_ok = _mock_response(200, {"status": "ok"})

        def fake_get(url, headers=None, timeout=None):
            if url.endswith("/pods/pod-abc"):
                return status_sequence.pop(0)
            if url.endswith("/health"):
                return health_ok
            raise AssertionError(f"unexpected GET to {url}")

        with (
            patch("scanning.pod_manager.requests.get", side_effect=fake_get),
            patch(
                "scanning.pod_manager.requests.post",
                return_value=_mock_response(200, {}),
            ) as mock_post,
            patch("scanning.pod_manager.time.sleep", return_value=None),
        ):
            base_url, boot_ms = pod_manager.ensure_pod_ready(timeout=30)

        self.assertEqual(base_url, "https://pod-abc-8000.proxy.runpod.net")
        self.assertGreaterEqual(boot_ms, 0)
        self.assertEqual(mock_post.call_count, 1)
        self.assertIn("/pods/pod-abc/start", mock_post.call_args[0][0])

    def test_boot_timeout_raises_PodBootTimeout(self):
        """If the pod never reaches RUNNING + /health=200, raise."""
        pod_exited = {"id": "pod-abc", "desiredStatus": "EXITED"}
        with (
            patch(
                "scanning.pod_manager.requests.get",
                return_value=_mock_response(200, pod_exited),
            ),
            patch(
                "scanning.pod_manager.requests.post",
                return_value=_mock_response(200, {}),
            ),
            patch("scanning.pod_manager.time.sleep", return_value=None),
        ):
            with self.assertRaises(pod_manager.PodBootTimeout):
                pod_manager.ensure_pod_ready(timeout=0)


@override_settings(
    RUNPOD_API_KEY="secret",
    RUNPOD_POD_ID="pod-abc",
)
class TestStopPod(TestCase):
    """pod_manager.stop_pod is idempotent."""

    def test_stop_pod_returns_on_200(self):
        with patch(
            "scanning.pod_manager.requests.post",
            return_value=_mock_response(200, {}),
        ):
            pod_manager.stop_pod()

    def test_stop_pod_tolerates_404_and_409(self):
        """Pod already stopped is not an error."""
        for code in (404, 409):
            with patch(
                "scanning.pod_manager.requests.post",
                return_value=_mock_response(code, {}),
            ):
                pod_manager.stop_pod()

    def test_stop_pod_raises_on_5xx(self):
        with patch(
            "scanning.pod_manager.requests.post",
            return_value=_mock_response(500, {}),
        ):
            with self.assertRaises(pod_manager.PodError):
                pod_manager.stop_pod()


class TestActivityTracking(TestCase):
    """record_activity / get_last_activity / clear_activity."""

    def setUp(self):
        cache.clear()

    def test_record_and_read_back(self):
        pod_manager.record_activity()
        got = pod_manager.get_last_activity()
        self.assertIsNotNone(got)
        self.assertLessEqual(abs((datetime.now(UTC) - got).total_seconds()), 5)

    def test_clear(self):
        pod_manager.record_activity()
        pod_manager.clear_activity()
        self.assertIsNone(pod_manager.get_last_activity())

    def test_empty_when_never_set(self):
        self.assertIsNone(pod_manager.get_last_activity())


@override_settings(
    RUNPOD_ENABLED=True,
    RUNPOD_API_KEY="secret",
    RUNPOD_POD_ID="pod-abc",
    RUNPOD_POD_IDLE_GRACE_SECONDS=60,
)
class TestStopIdleGpuPodCommand(TestCase):
    """stop_idle_gpu_pod only stops the pod when the queue is truly idle."""

    def setUp(self):
        cache.clear()

    def test_noop_when_scan_is_queued(self):
        ScanFactory(status=Status.QUEUED)
        with patch("scanning.pod_manager.stop_pod") as mock_stop:
            StopIdleCommand().handle()
        mock_stop.assert_not_called()

    def test_noop_when_scan_is_processing(self):
        ScanFactory(status=Status.PROCESSING)
        with patch("scanning.pod_manager.stop_pod") as mock_stop:
            StopIdleCommand().handle()
        mock_stop.assert_not_called()

    def test_noop_when_within_grace_window(self):
        """Fresh activity means we don't stop yet, even with an empty queue."""
        pod_manager.record_activity()
        with patch("scanning.pod_manager.stop_pod") as mock_stop:
            StopIdleCommand().handle()
        mock_stop.assert_not_called()

    def test_stops_pod_when_idle_past_grace_and_running(self):
        """Old activity + empty queue + RUNNING pod -> stop."""
        old = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
        cache.set("scanning:runpod_pod:last_activity", old, timeout=3600)
        pod_running = {"id": "pod-abc", "desiredStatus": "RUNNING"}
        with (
            patch(
                "scanning.pod_manager.get_pod_status",
                return_value=pod_running,
            ),
            patch("scanning.pod_manager.stop_pod") as mock_stop,
        ):
            StopIdleCommand().handle()
        mock_stop.assert_called_once()

    def test_noop_when_pod_already_stopped(self):
        """Pod is EXITED -> clear activity, don't call stop again."""
        old = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
        cache.set("scanning:runpod_pod:last_activity", old, timeout=3600)
        pod_exited = {"id": "pod-abc", "desiredStatus": "EXITED"}
        with (
            patch(
                "scanning.pod_manager.get_pod_status",
                return_value=pod_exited,
            ),
            patch("scanning.pod_manager.stop_pod") as mock_stop,
        ):
            StopIdleCommand().handle()
        mock_stop.assert_not_called()
        self.assertIsNone(pod_manager.get_last_activity())

    @override_settings(RUNPOD_ENABLED=False)
    def test_disabled_is_noop(self):
        """When RUNPOD_ENABLED is False the command is a noop."""
        with (
            patch("scanning.pod_manager.get_pod_status") as mock_status,
            patch("scanning.pod_manager.stop_pod") as mock_stop,
        ):
            StopIdleCommand().handle()
        mock_status.assert_not_called()
        mock_stop.assert_not_called()


@override_settings(
    RUNPOD_ENABLED=True,
    RUNPOD_API_KEY="secret",
    RUNPOD_POD_ID="pod-abc",
    RUNPOD_POD_API_KEY="bearer",
    RUNPOD_POD_PORT=8000,
)
class TestRunpodClientPodTransport(TestCase):
    """runpod_client._invoke against a stubbed pod HTTP server."""

    def setUp(self):
        cache.clear()

    def _scan(self):
        return ScanFactory(pk=42)

    def test_happy_path_returns_body(self):
        expected = {
            "detections": [{"page_index": 0}],
            "page_count": 1,
            "metrics": {"total_ms": 1000},
        }
        with (
            patch(
                "scanning.runpod_client.pod_manager.ensure_pod_ready",
                return_value=("https://pod-abc-8000.proxy.runpod.net", 5000),
            ),
            patch("scanning.runpod_client.pod_manager.record_activity"),
            patch(
                "scanning.runpod_client.requests.post",
                return_value=_mock_response(200, expected),
            ) as mock_post,
        ):
            result = runpod_client._invoke(
                action="detect",
                scan=self._scan(),
                payload={"pdf_url": "https://x/y.pdf"},
                progress_callback=None,
            )

        self.assertEqual(result, expected)
        # Body includes scan_pk for Sentry tagging on the pod.
        sent = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent["scan_pk"], 42)
        # Bearer auth is set.
        self.assertEqual(
            mock_post.call_args.kwargs["headers"]["Authorization"],
            "Bearer bearer",
        )

    def test_NO_GPU_becomes_RunpodTransientError(self):
        err_body = {"error": "no gpu", "error_code": "NO_GPU"}
        with (
            patch(
                "scanning.runpod_client.pod_manager.ensure_pod_ready",
                return_value=("https://pod-abc-8000.proxy.runpod.net", 0),
            ),
            patch("scanning.runpod_client.pod_manager.record_activity"),
            patch(
                "scanning.runpod_client.requests.post",
                return_value=_mock_response(503, err_body),
            ),
        ):
            with self.assertRaises(runpod_client.RunpodTransientError):
                runpod_client._invoke(
                    action="detect",
                    scan=self._scan(),
                    payload={"pdf_url": "https://x/y.pdf"},
                    progress_callback=None,
                )

    def test_boot_timeout_becomes_RunpodTransientError(self):
        with patch(
            "scanning.runpod_client.pod_manager.ensure_pod_ready",
            side_effect=pod_manager.PodBootTimeout("boot timed out"),
        ):
            with self.assertRaises(runpod_client.RunpodTransientError):
                runpod_client._invoke(
                    action="detect",
                    scan=self._scan(),
                    payload={"pdf_url": "https://x/y.pdf"},
                    progress_callback=None,
                )

    def test_action_failed_becomes_RunpodError(self):
        """Non-transient structured errors mark the scan ERROR."""
        err_body = {"error": "boom", "error_code": "ACTION_FAILED"}
        with (
            patch(
                "scanning.runpod_client.pod_manager.ensure_pod_ready",
                return_value=("https://pod-abc-8000.proxy.runpod.net", 0),
            ),
            patch("scanning.runpod_client.pod_manager.record_activity"),
            patch(
                "scanning.runpod_client.requests.post",
                return_value=_mock_response(500, err_body),
            ),
        ):
            with self.assertRaises(runpod_client.RunpodError) as ctx:
                runpod_client._invoke(
                    action="detect",
                    scan=self._scan(),
                    payload={"pdf_url": "https://x/y.pdf"},
                    progress_callback=None,
                )
            # Not the transient subclass.
            self.assertNotIsInstance(
                ctx.exception, runpod_client.RunpodTransientError
            )

    def test_connection_error_retries_then_raises_transient(self):
        import requests as req_mod

        with (
            patch(
                "scanning.runpod_client.pod_manager.ensure_pod_ready",
                return_value=("https://pod-abc-8000.proxy.runpod.net", 0),
            ),
            patch("scanning.runpod_client.pod_manager.record_activity"),
            patch(
                "scanning.runpod_client.requests.post",
                side_effect=req_mod.ConnectionError("refused"),
            ),
            patch("scanning.runpod_client.time.sleep", return_value=None),
        ):
            with self.assertRaises(runpod_client.RunpodTransientError):
                runpod_client._invoke(
                    action="detect",
                    scan=self._scan(),
                    payload={"pdf_url": "https://x/y.pdf"},
                    progress_callback=None,
                )
