"""Tests for ``scanning.runpod_client``.

All RunPod HTTP traffic is mocked at the ``requests`` layer; nothing
here hits the network or the RunPod API. Local-fallback paths mock
blackletter's entry points so the tests don't need GPU or large
PDFs.

Covers:

- ``_redact_urls`` masking behaviour.
- ``_ensure_presigned_url`` upload / head-object / error classification.
- ``_submit`` retry logic and malformed-response handling.
- ``_poll`` terminal states, transient classification, deadline expiry,
  structured-error mapping, and the ``head_object`` check that salvages
  a finished job whose ``/status`` record has 404'd.
- ``_invoke`` body construction and missing-credential guard.
- ``submit_job`` / ``poll_once``, the two non-blocking halves the batch
  daemon drives: that neither waits on the other, and how one
  ``/status`` answer maps onto ``JobStatus``.
- Result delivery via presigned PUT: key/URL minting, the freshness
  check that keeps a reused key from serving a previous attempt's
  output, envelope validation, and the inline path.
- Public ``detect`` / ``analyze`` local fallback + remote dispatch.
"""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import requests
from botocore.exceptions import ClientError, ResponseStreamingError
from django.test import TestCase, override_settings

from scanning import runpod_client
from scanning.factories import ScanFactory
from scanning.models import JobStatus


def _mock_response(status_code: int, body: dict | None = None):
    """Build a stand-in for a ``requests.Response``.

    :param status_code: HTTP status.
    :param body: Parsed JSON body. ``None`` means ``.json()`` raises
        ``ValueError`` (for malformed-body tests).
    :returns: MagicMock that behaves like a Response.
    :rtype: MagicMock
    """
    r = MagicMock()
    r.status_code = status_code
    if body is None:
        r.json.side_effect = ValueError("no json body")
        r.text = ""
    else:
        r.json.return_value = body
        r.text = str(body)
    if 400 <= status_code:
        r.raise_for_status.side_effect = requests.HTTPError(
            f"{status_code} error", response=r
        )
    else:
        r.raise_for_status.return_value = None
    return r


def _client_error(code: str) -> ClientError:
    """Build a botocore ``ClientError`` with a specific error code.

    :param code: Value to put in ``response['Error']['Code']``.
    :returns: The exception.
    :rtype: ClientError
    """
    return ClientError(
        {"Error": {"Code": code, "Message": code}}, "HeadObject"
    )


# ── Pure helpers ────────────────────────────────────────────────────
class TestRedactUrls(TestCase):
    """``_redact_urls`` masks presigned URLs without mutating input."""

    def test_masks_pdf_url_under_input(self):
        body = {
            "input": {
                "action": "detect",
                "scan_pk": 42,
                "pdf_url": "https://s3.example/secret?X-Amz-Signature=abc",
                "models": ["small"],
            }
        }
        redacted = runpod_client._redact_urls(body)
        self.assertEqual(redacted["input"]["pdf_url"], "***")

    def test_masks_result_url_but_keeps_result_key(self):
        body = {
            "input": {
                "action": "detect",
                "result_url": "https://s3.example/out?X-Amz-Signature=abc",
                "result_key": "processing/1/x/jobs/detect/result.json",
            }
        }
        redacted = runpod_client._redact_urls(body)
        self.assertEqual(redacted["input"]["result_url"], "***")
        self.assertEqual(
            redacted["input"]["result_key"],
            "processing/1/x/jobs/detect/result.json",
        )

    def test_preserves_other_input_keys(self):
        body = {
            "input": {
                "action": "detect",
                "scan_pk": 42,
                "pdf_url": "https://...",
                "models": ["small"],
                "confidence": 0.2,
            }
        }
        redacted = runpod_client._redact_urls(body)
        self.assertEqual(redacted["input"]["action"], "detect")
        self.assertEqual(redacted["input"]["scan_pk"], 42)
        self.assertEqual(redacted["input"]["models"], ["small"])
        self.assertEqual(redacted["input"]["confidence"], 0.2)

    def test_does_not_mutate_original(self):
        body = {
            "input": {
                "pdf_url": "https://s3.example/x",
                "result_url": "https://s3.example/y",
            }
        }
        runpod_client._redact_urls(body)
        self.assertEqual(body["input"]["pdf_url"], "https://s3.example/x")
        self.assertEqual(body["input"]["result_url"], "https://s3.example/y")

    def test_no_urls_no_change(self):
        body = {"input": {"action": "detect", "scan_pk": 42}}
        redacted = runpod_client._redact_urls(body)
        self.assertNotIn("pdf_url", redacted["input"])
        self.assertNotIn("result_url", redacted["input"])

    def test_non_dict_input_not_touched(self):
        body = {"input": "not a dict"}
        redacted = runpod_client._redact_urls(body)
        self.assertEqual(redacted["input"], "not a dict")


# ── _ensure_presigned_url ───────────────────────────────────────────
@override_settings(
    RUNPOD_ENABLED=True,
    AWS_PRIVATE_STORAGE_BUCKET_NAME="bucket",
    RUNPOD_PRESIGNED_TTL=3600,
)
class TestEnsurePresignedUrl(TestCase):
    """``_ensure_presigned_url`` covers the head_object branches
    introduced by the ClientError-scoping fix.
    """

    def _setup_s3(self, head_result=None):
        """Build a boto3 client mock with head_object + presign stubs.

        :param head_result: If an Exception instance, raised by
            head_object. If a dict, returned. If None, returns {}.
        :returns: The mock boto3 client.
        """
        s3 = MagicMock()
        if isinstance(head_result, Exception):
            s3.head_object.side_effect = head_result
        else:
            s3.head_object.return_value = head_result or {}
        s3.generate_presigned_url.return_value = "https://signed.example/x"
        return s3

    def _patch(self, s3):
        """Patch the collaborators _ensure_presigned_url touches."""
        return [
            patch("scanning.runpod_client.boto3.client", return_value=s3),
            patch("scanning.utils.has_s3_credentials", return_value=True),
            patch(
                "scanning.s3_sync.s3_processing_prefix",
                return_value="processing/42/",
            ),
        ]

    def test_raises_when_no_aws_creds(self):
        scan = ScanFactory()
        with patch("scanning.utils.has_s3_credentials", return_value=False):
            with self.assertRaises(runpod_client.RunpodError):
                runpod_client._ensure_presigned_url(scan, "/tmp/x.pdf")

    def test_head_object_hits_returns_presigned_url_without_upload(self):
        scan = ScanFactory()
        s3 = self._setup_s3()  # head_object succeeds with {}
        patches = self._patch(s3)
        for p in patches:
            p.start()
        try:
            url = runpod_client._ensure_presigned_url(scan, "/tmp/x.pdf")
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(url, "https://signed.example/x")
        s3.upload_file.assert_not_called()

    def test_404_triggers_upload_then_presign(self):
        scan = ScanFactory()
        s3 = self._setup_s3(head_result=_client_error("404"))
        patches = self._patch(s3)
        # Pretend the local file exists so the upload path doesn't
        # raise FileNotFoundError.
        for p in patches:
            p.start()
        try:
            with patch(
                "scanning.runpod_client.Path.is_file", return_value=True
            ):
                url = runpod_client._ensure_presigned_url(scan, "/tmp/x.pdf")
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(url, "https://signed.example/x")
        s3.upload_file.assert_called_once()

    def test_no_such_key_also_triggers_upload(self):
        """Some S3 configs return NoSuchKey instead of 404."""
        scan = ScanFactory()
        s3 = self._setup_s3(head_result=_client_error("NoSuchKey"))
        patches = self._patch(s3)
        for p in patches:
            p.start()
        try:
            with patch(
                "scanning.runpod_client.Path.is_file", return_value=True
            ):
                runpod_client._ensure_presigned_url(scan, "/tmp/x.pdf")
        finally:
            for p in patches:
                p.stop()
        s3.upload_file.assert_called_once()

    def test_not_found_also_triggers_upload(self):
        """Several S3-compatible backends spell absence ``NotFound``.

        This site used to check only ``("404", "NoSuchKey")`` while the
        two result-object checks also accepted ``NotFound``, so the same
        response meant "upload it" in one place and "S3 is broken" here.
        """
        scan = ScanFactory()
        s3 = self._setup_s3(head_result=_client_error("NotFound"))
        patches = self._patch(s3)
        for p in patches:
            p.start()
        try:
            with patch(
                "scanning.runpod_client.Path.is_file", return_value=True
            ):
                runpod_client._ensure_presigned_url(scan, "/tmp/x.pdf")
        finally:
            for p in patches:
                p.stop()
        s3.upload_file.assert_called_once()

    def test_access_denied_re_raises(self):
        """Any ClientError other than 404/NoSuchKey must surface."""
        scan = ScanFactory()
        s3 = self._setup_s3(head_result=_client_error("AccessDenied"))
        patches = self._patch(s3)
        for p in patches:
            p.start()
        try:
            with self.assertRaises(ClientError):
                runpod_client._ensure_presigned_url(scan, "/tmp/x.pdf")
        finally:
            for p in patches:
                p.stop()
        s3.upload_file.assert_not_called()

    def test_missing_local_file_raises_file_not_found(self):
        scan = ScanFactory()
        s3 = self._setup_s3(head_result=_client_error("404"))
        patches = self._patch(s3)
        for p in patches:
            p.start()
        try:
            with patch(
                "scanning.runpod_client.Path.is_file", return_value=False
            ):
                with self.assertRaises(FileNotFoundError):
                    runpod_client._ensure_presigned_url(scan, "/tmp/x.pdf")
        finally:
            for p in patches:
                p.stop()


# ── _submit ─────────────────────────────────────────────────────────
class TestSubmit(TestCase):
    """``_submit`` retry + response-shape handling."""

    def test_returns_job_id_on_200(self):
        with patch(
            "scanning.runpod_client.requests.post",
            return_value=_mock_response(200, {"id": "job-123"}),
        ):
            job_id = runpod_client._submit(
                base_url="https://api/run/endpoint",
                headers={"Authorization": "Bearer k"},
                body={"input": {"action": "detect", "pdf_url": "x"}},
                action="detect",
                max_retries=2,
                progress_callback=None,
            )
        self.assertEqual(job_id, "job-123")

    def test_malformed_response_no_id_raises(self):
        with (
            patch(
                "scanning.runpod_client.requests.post",
                return_value=_mock_response(200, {"status": "queued"}),
            ),
            patch("scanning.runpod_client.time.sleep"),
        ):
            with self.assertRaises(runpod_client.RunpodError):
                runpod_client._submit(
                    base_url="https://api/run/endpoint",
                    headers={},
                    body={"input": {}},
                    action="detect",
                    max_retries=1,
                    progress_callback=None,
                )

    def test_retries_on_connection_error_then_succeeds(self):
        responses = [
            requests.ConnectionError("refused"),
            _mock_response(200, {"id": "job-456"}),
        ]

        def side_effect(*args, **kwargs):
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        with (
            patch(
                "scanning.runpod_client.requests.post",
                side_effect=side_effect,
            ),
            patch("scanning.runpod_client.time.sleep"),
        ):
            job_id = runpod_client._submit(
                base_url="https://api/run/endpoint",
                headers={},
                body={"input": {}},
                action="detect",
                max_retries=2,
                progress_callback=None,
            )
        self.assertEqual(job_id, "job-456")

    def test_exhausted_retries_raise_runpod_error(self):
        with (
            patch(
                "scanning.runpod_client.requests.post",
                side_effect=requests.ConnectionError("refused"),
            ),
            patch("scanning.runpod_client.time.sleep"),
        ):
            with self.assertRaises(runpod_client.RunpodError) as ctx:
                runpod_client._submit(
                    base_url="https://api/run/endpoint",
                    headers={},
                    body={"input": {}},
                    action="detect",
                    max_retries=1,
                    progress_callback=None,
                )
        # A transport error carries no response, so it stays terminal.
        self.assertNotIsInstance(
            ctx.exception, runpod_client.RunpodTransientError
        )

    def test_endpoint_paused_409_raises_transient(self):
        """A 409 ENDPOINT_PAUSED re-queues the scan rather than failing."""
        body = {
            "status": 409,
            "title": "Conflict",
            "detail": (
                "Endpoint is paused (max_workers=0). "
                "Set max_workers > 0 to accept work."
            ),
            "code": "ENDPOINT_PAUSED",
        }
        with (
            patch(
                "scanning.runpod_client.requests.post",
                return_value=_mock_response(409, body),
            ),
            patch("scanning.runpod_client.time.sleep"),
        ):
            with self.assertRaises(runpod_client.RunpodTransientError):
                runpod_client._submit(
                    base_url="https://api/run/endpoint",
                    headers={},
                    body={"input": {}},
                    action="detect",
                    max_retries=1,
                    progress_callback=None,
                )

    def test_409_without_known_code_still_transient(self):
        """Any 409 re-queues, even without an ENDPOINT_PAUSED code."""
        with (
            patch(
                "scanning.runpod_client.requests.post",
                return_value=_mock_response(409, {"title": "Conflict"}),
            ),
            patch("scanning.runpod_client.time.sleep"),
        ):
            with self.assertRaises(runpod_client.RunpodTransientError):
                runpod_client._submit(
                    base_url="https://api/run/endpoint",
                    headers={},
                    body={"input": {}},
                    action="detect",
                    max_retries=1,
                    progress_callback=None,
                )

    def test_paused_endpoint_short_circuits_without_retrying(self):
        """A 409 raises immediately instead of burning the retry budget."""
        with (
            patch(
                "scanning.runpod_client.requests.post",
                return_value=_mock_response(409, {"code": "ENDPOINT_PAUSED"}),
            ) as mock_post,
            patch("scanning.runpod_client.time.sleep") as mock_sleep,
        ):
            with self.assertRaises(runpod_client.RunpodTransientError):
                runpod_client._submit(
                    base_url="https://api/run/endpoint",
                    headers={},
                    body={"input": {}},
                    action="detect",
                    max_retries=2,
                    progress_callback=None,
                )
        self.assertEqual(mock_post.call_count, 1)
        mock_sleep.assert_not_called()

    def test_non_409_http_error_stays_terminal(self):
        """A non-409 HTTP error is terminal, not a re-queue."""
        with (
            patch(
                "scanning.runpod_client.requests.post",
                return_value=_mock_response(400, {"code": "BAD_INPUT"}),
            ),
            patch("scanning.runpod_client.time.sleep"),
        ):
            with self.assertRaises(runpod_client.RunpodError) as ctx:
                runpod_client._submit(
                    base_url="https://api/run/endpoint",
                    headers={},
                    body={"input": {}},
                    action="detect",
                    max_retries=1,
                    progress_callback=None,
                )
        self.assertNotIsInstance(
            ctx.exception, runpod_client.RunpodTransientError
        )

    def test_logs_response_body_on_failure(self):
        """The RunPod error body is logged, not swallowed by raise_for_status."""
        body = {"code": "ENDPOINT_PAUSED", "detail": "Endpoint is paused"}
        with (
            patch(
                "scanning.runpod_client.requests.post",
                return_value=_mock_response(409, body),
            ),
            patch("scanning.runpod_client.time.sleep"),
            self.assertLogs("scanning.runpod_client", level="WARNING") as logs,
        ):
            with self.assertRaises(runpod_client.RunpodError):
                runpod_client._submit(
                    base_url="https://api/run/endpoint",
                    headers={},
                    body={"input": {}},
                    action="detect",
                    max_retries=0,
                    progress_callback=None,
                )
        self.assertTrue(
            any("Endpoint is paused" in line for line in logs.output)
        )


# ── _poll ───────────────────────────────────────────────────────────
class TestPoll(TestCase):
    """``_poll`` state classification and backoff behaviour."""

    def _poll(
        self,
        status_responses,
        deadline_offset: float = 600.0,
    ):
        """Run ``_poll`` against a stubbed ``requests.get``.

        :param status_responses: List of (status_code, body_dict)
            tuples returned in order by requests.get.
        :param deadline_offset: Seconds the deadline is in the future
            from ``time.monotonic()``.
        :returns: The return value of _poll (or raises).
        """
        import time as _time

        mocks = [_mock_response(c, b) for c, b in status_responses]
        with (
            patch("scanning.runpod_client.requests.get", side_effect=mocks),
            patch(
                "scanning.runpod_client.requests.post",
                return_value=_mock_response(200, {}),
            ),
            patch("scanning.runpod_client.time.sleep", return_value=None),
        ):
            return runpod_client._poll(
                base_url="https://api/run/endpoint",
                headers={},
                job_id="job-1",
                action="detect",
                deadline=_time.monotonic() + deadline_offset,
                progress_callback=None,
            )

    def test_completed_returns_output(self):
        output = {"detections": [{"page_index": 0}], "duration_ms": 100}
        result = self._poll(
            [
                (200, {"status": "IN_QUEUE"}),
                (200, {"status": "COMPLETED", "output": output}),
            ]
        )
        self.assertEqual(result, output)

    def test_404_raises_transient(self):
        with self.assertRaises(runpod_client.RunpodTransientError):
            self._poll([(404, None)])

    def test_no_gpu_output_raises_transient(self):
        # Handler returns {"error": ..., "error_code": "NO_GPU", ...}.
        # The SDK moves "error" to the top level; RunPod marks the job
        # FAILED. The daemon reads error_code from output to raise
        # RunpodTransientError so the scan is re-queued.
        with self.assertRaises(runpod_client.RunpodTransientError):
            self._poll(
                [
                    (
                        200,
                        {
                            "status": "FAILED",
                            "error": "no gpu",
                            "output": {"error_code": "NO_GPU"},
                        },
                    )
                ]
            )

    def test_non_transient_handler_error_raises_runpod_error(self):
        # Handler errors with non-transient codes (BAD_INPUT, UNKNOWN_ACTION)
        # also arrive as FAILED but raise the base RunpodError, not the
        # transient subclass, so the scan is marked ERROR rather than re-queued.
        with self.assertRaises(runpod_client.RunpodError) as ctx:
            self._poll(
                [
                    (
                        200,
                        {
                            "status": "FAILED",
                            "error": "bad input",
                            "output": {"error_code": "BAD_INPUT"},
                        },
                    )
                ]
            )
        # It must NOT be the transient subclass.
        self.assertNotIsInstance(
            ctx.exception, runpod_client.RunpodTransientError
        )

    def test_result_upload_codes_are_classified_by_cause(self):
        # The two delivery failures a re-run can fix re-queue the scan;
        # the one that means "S3 refused this write" does not, because
        # each retry pays for another GPU run to fail identically.
        cases = {
            "RESULT_UPLOAD_FAILED": True,
            "RESULT_URL_EXPIRED": True,
            "RESULT_UPLOAD_REJECTED": False,
        }
        for code, is_transient in cases.items():
            with self.subTest(code=code):
                with self.assertRaises(runpod_client.RunpodError) as ctx:
                    self._poll(
                        [
                            (
                                200,
                                {
                                    "status": "FAILED",
                                    "error": "upload",
                                    "output": {"error_code": code},
                                },
                            )
                        ]
                    )
                self.assertEqual(
                    isinstance(
                        ctx.exception, runpod_client.RunpodTransientError
                    ),
                    is_transient,
                )

    def test_failed_with_no_output_raises_transient(self):
        # RunPod platform failures (worker timeout, "job timed out after N
        # retries", worker crash) arrive as FAILED with no output field.
        # These are infrastructure problems, not handler logic failures, so
        # re-queue rather than permanently failing the scan.
        with self.assertRaises(runpod_client.RunpodTransientError):
            self._poll(
                [
                    (
                        200,
                        {
                            "status": "FAILED",
                            "error": "job timed out after 1 retries",
                            "retries": 1,
                        },
                    )
                ]
            )

    def test_terminal_failure_statuses_raise(self):
        """TIMED_OUT / CANCELLED end the poll as RunpodError.

        FAILED without output is transient (platform failure); FAILED
        with output.error_code is handled by the structured-error tests.
        """
        for status in ("TIMED_OUT", "CANCELLED"):
            with self.subTest(status=status):
                with self.assertRaises(runpod_client.RunpodError):
                    self._poll([(200, {"status": status, "error": "boom"})])

    def test_non_dict_output_raises(self):
        with self.assertRaises(runpod_client.RunpodError):
            self._poll([(200, {"status": "COMPLETED", "output": "a string"})])

    def test_deadline_exceeded_raises_and_cancels(self):
        """When monotonic > deadline, _poll calls /cancel and raises."""
        import time as _time

        # Don't use the _poll helper here: it adds its own patches
        # that shadow the requests.post mock we need to inspect.
        with (
            patch(
                "scanning.runpod_client.requests.post",
                return_value=_mock_response(200, {}),
            ) as mock_post,
            patch("scanning.runpod_client.time.sleep", return_value=None),
        ):
            with self.assertRaises(runpod_client.RunpodError):
                runpod_client._poll(
                    base_url="https://api/run/endpoint",
                    headers={},
                    job_id="job-1",
                    action="detect",
                    deadline=_time.monotonic() - 1.0,
                    progress_callback=None,
                )
            # At least one POST to /cancel/{job_id}.
            called_urls = [c.args[0] for c in mock_post.call_args_list]
            self.assertTrue(any("/cancel/" in u for u in called_urls))


# ── _invoke ─────────────────────────────────────────────────────────
@override_settings(
    RUNPOD_ENABLED=True,
    RUNPOD_ENDPOINT_ID="ep-abc",
    RUNPOD_API_KEY="apikey",
    RUNPOD_REQUEST_TIMEOUT=900,
    RUNPOD_MAX_RETRIES=0,
)
class TestInvoke(TestCase):
    """``_invoke`` body shape and missing-config guards.

    Pinned to inline mode (no AWS credentials, so nothing to presign a
    PUT with): no ``result_url`` goes into the job input and the
    handler's output is returned as-is. The S3 path has its own test
    classes below.
    """

    def setUp(self):
        patcher = patch(
            "scanning.runpod_client._results_to_s3_enabled",
            return_value=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_builds_body_with_scan_pk_and_passes_to_submit(self):
        scan = ScanFactory()
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["body"] = json
            return _mock_response(200, {"id": "job-xyz"})

        output = {"detections": [], "duration_ms": 1}
        statuses = [
            _mock_response(200, {"status": "COMPLETED", "output": output})
        ]
        with (
            patch(
                "scanning.runpod_client.requests.post", side_effect=fake_post
            ),
            patch("scanning.runpod_client.requests.get", side_effect=statuses),
            patch("scanning.runpod_client.time.sleep", return_value=None),
        ):
            result = runpod_client._invoke(
                action="detect",
                scan=scan,
                payload={"pdf_url": "https://x/y.pdf", "models": ["small"]},
                progress_callback=None,
            )

        self.assertEqual(result, output)
        self.assertIn("/run/", captured["url"] + "/")  # hits /run
        self.assertEqual(captured["body"]["input"]["action"], "detect")
        self.assertEqual(captured["body"]["input"]["scan_pk"], scan.pk)
        self.assertEqual(
            captured["body"]["input"]["pdf_url"], "https://x/y.pdf"
        )
        self.assertNotIn("result_url", captured["body"]["input"])
        self.assertNotIn("result_key", captured["body"]["input"])

    @override_settings(RUNPOD_ENDPOINT_ID="")
    def test_missing_endpoint_id_raises(self):
        scan = ScanFactory()
        with self.assertRaises(runpod_client.RunpodError):
            runpod_client._invoke(
                action="detect",
                scan=scan,
                payload={"pdf_url": "x"},
                progress_callback=None,
            )

    @override_settings(RUNPOD_API_KEY="")
    def test_missing_api_key_raises(self):
        scan = ScanFactory()
        with self.assertRaises(runpod_client.RunpodError):
            runpod_client._invoke(
                action="detect",
                scan=scan,
                payload={"pdf_url": "x"},
                progress_callback=None,
            )


# ── Public API ──────────────────────────────────────────────────────
class TestDetectLocalFallback(TestCase):
    """``detect()`` with RUNPOD_ENABLED=False uses blackletter directly."""

    @override_settings(RUNPOD_ENABLED=False)
    def test_falls_back_to_bl_detect(self):
        scan = ScanFactory()
        with patch(
            "blackletter.api.detect",
            return_value=[{"page_index": 0, "label": "CASE_CAPTION"}],
        ) as mock_detect:
            result = runpod_client.detect(
                scan, "/tmp/fake.pdf", models=["small"]
            )
        self.assertEqual(len(result), 1)
        mock_detect.assert_called_once()
        args, kwargs = mock_detect.call_args
        self.assertEqual(args[0], "/tmp/fake.pdf")
        self.assertEqual(kwargs["models"], ["small"])


class TestAnalyzeLocalFallback(TestCase):
    """``analyze()`` with RUNPOD_ENABLED=False uses blackletter directly."""

    @override_settings(RUNPOD_ENABLED=False)
    def test_falls_back_to_bl_analyze_pdf(self):
        scan = ScanFactory()
        with patch(
            "blackletter.analyze.analyze_pdf",
            return_value={"results": [{"pdf_page": 1}]},
        ) as mock_analyze:
            result = runpod_client.analyze(
                scan,
                "/tmp/fake.pdf",
                exp_start=1,
                exp_end=10,
            )
        self.assertEqual(result, {"results": [{"pdf_page": 1}]})
        mock_analyze.assert_called_once()


@override_settings(
    RUNPOD_ENABLED=True,
    RUNPOD_ENDPOINT_ID="ep-abc",
    RUNPOD_API_KEY="apikey",
    RUNPOD_REQUEST_TIMEOUT=900,
    RUNPOD_MAX_RETRIES=0,
    AWS_PRIVATE_STORAGE_BUCKET_NAME="bucket",
    RUNPOD_PRESIGNED_TTL=3600,
)
class TestDetectRemote(TestCase):
    """``detect()`` with RUNPOD_ENABLED=True walks presign + submit + poll.

    Inline mode, pinned as in :class:`TestInvoke`.
    """

    def setUp(self):
        patcher = patch(
            "scanning.runpod_client._results_to_s3_enabled",
            return_value=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_remote_detect_happy_path(self):
        scan = ScanFactory()
        output = {
            "detections": [{"page_index": 0, "label": "CASE_CAPTION"}],
            "page_count": 1,
            "duration_ms": 42,
        }
        with (
            patch(
                "scanning.runpod_client._ensure_presigned_url",
                return_value="https://signed.example/bitonal.pdf",
            ),
            patch(
                "scanning.runpod_client.requests.post",
                return_value=_mock_response(200, {"id": "job-1"}),
            ),
            patch(
                "scanning.runpod_client.requests.get",
                return_value=_mock_response(
                    200, {"status": "COMPLETED", "output": output}
                ),
            ),
            patch("scanning.runpod_client.time.sleep", return_value=None),
        ):
            result = runpod_client.detect(
                scan, "/tmp/fake.pdf", models=["small"]
            )
        # detect() unwraps output["detections"]
        self.assertEqual(result, output["detections"])


@override_settings(
    RUNPOD_ENABLED=True,
    RUNPOD_ENDPOINT_ID="ep-abc",
    RUNPOD_API_KEY="apikey",
    RUNPOD_REQUEST_TIMEOUT=900,
    RUNPOD_MAX_RETRIES=0,
    AWS_PRIVATE_STORAGE_BUCKET_NAME="bucket",
    RUNPOD_PRESIGNED_TTL=3600,
)
class TestAnalyzeRemote(TestCase):
    """``analyze()`` with RUNPOD_ENABLED=True walks presign + submit + poll.

    Inline mode, pinned as in :class:`TestInvoke`.
    """

    def setUp(self):
        patcher = patch(
            "scanning.runpod_client._results_to_s3_enabled",
            return_value=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_remote_analyze_happy_path(self):
        scan = ScanFactory()
        worker_output = {
            "results": [{"pdf_page": 1, "detected": "1"}],
            "page_count": 1,
            "duration_ms": 42,
        }
        with (
            patch(
                "scanning.runpod_client._ensure_presigned_url",
                return_value="https://signed.example/orig.pdf",
            ),
            patch(
                "scanning.runpod_client.requests.post",
                return_value=_mock_response(200, {"id": "job-2"}),
            ),
            patch(
                "scanning.runpod_client.requests.get",
                return_value=_mock_response(
                    200, {"status": "COMPLETED", "output": worker_output}
                ),
            ),
            patch("scanning.runpod_client.time.sleep", return_value=None),
        ):
            result = runpod_client.analyze(
                scan,
                "/tmp/fake.pdf",
                exp_start=1,
                exp_end=1,
            )
        # analyze() returns {"results": [...]} discarding extras.
        self.assertEqual(result, {"results": worker_output["results"]})


# ── Result delivery via presigned PUT ───────────────────────────────
# Result keys are reused across runs, so every read is gated on the
# object having been written after the reading job was submitted.
_SUBMITTED_AT = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
_FRESH = _SUBMITTED_AT + timedelta(seconds=30)
_STALE = _SUBMITTED_AT - timedelta(hours=2)


_JOB_ID = "job-1"


def _envelope(
    action="detect",
    scan_pk=1,
    payload=None,
    schema_version=runpod_client.RESULT_SCHEMA_VERSION,
    job_id=_JOB_ID,
):
    """Build a result envelope the way the worker writes it.

    :param action: Action that produced the payload.
    :param scan_pk: Scan the job belongs to.
    :param payload: The action's result dict.
    :param schema_version: Envelope version to claim.
    :param job_id: RunPod job to claim wrote it.
    :returns: The envelope dict.
    :rtype: dict
    """
    return {
        "schema_version": schema_version,
        "action": action,
        "scan_pk": scan_pk,
        "job_id": job_id,
        "payload": payload if payload is not None else {"detections": []},
    }


def _body_stub(raw: bytes):
    """Wrap raw bytes as a ``get_object`` response body."""
    body = MagicMock()
    body.read.return_value = raw
    return {"Body": body}


def _s3_stub(envelope=None, head_error=None, last_modified=_FRESH):
    """Return a stub S3 client serving one result object.

    :param envelope: Object body ``get_object`` returns, JSON-encoded.
    :param head_error: ``ClientError`` ``head_object`` should raise.
    :param last_modified: ``LastModified`` ``head_object`` reports.
        Defaults to just after :data:`_SUBMITTED_AT`, i.e. an object
        this run wrote.
    :returns: MagicMock standing in for a boto3 S3 client.
    :rtype: MagicMock
    """
    s3 = MagicMock()
    s3.generate_presigned_url.return_value = "https://signed.example/put"
    if head_error is not None:
        s3.head_object.side_effect = head_error
    else:
        s3.head_object.return_value = {"LastModified": last_modified}
    if envelope is not None:
        s3.get_object.return_value = _body_stub(json.dumps(envelope).encode())
    return s3


class TestResultsToS3Enabled(TestCase):
    """``_results_to_s3_enabled`` gates on AWS credentials.

    This is what keeps CI and credential-less dev off the S3 path, so
    it's tested directly rather than patched out (as the classes below
    do).
    """

    def _enabled(self, creds):
        with patch("scanning.utils.has_s3_credentials", return_value=creds):
            return runpod_client._results_to_s3_enabled()

    def test_on_with_credentials(self):
        self.assertTrue(self._enabled(True))

    def test_off_without_credentials(self):
        # Nothing to presign with, so the worker must answer inline.
        self.assertFalse(self._enabled(False))


@override_settings(
    AWS_PRIVATE_STORAGE_BUCKET_NAME="bucket",
    RUNPOD_PRESIGNED_TTL=3600,
)
class TestPresignResultPut(TestCase):
    """``_presign_result_put`` names the key and signs a PUT for it."""

    def test_key_is_per_scan_and_action(self):
        from scanning import s3_sync

        scan = ScanFactory()
        s3 = _s3_stub()
        with patch("scanning.runpod_client.boto3.client", return_value=s3):
            key, url = runpod_client._presign_result_put(scan, "detect")
            again, _ = runpod_client._presign_result_put(scan, "detect")
            analyze_key, _ = runpod_client._presign_result_put(scan, "analyze")

        prefix = s3_sync.s3_processing_prefix(scan)
        self.assertEqual(key, f"{prefix}jobs/detect/result.json")
        self.assertEqual(url, "https://signed.example/put")
        # Stable across submissions, so a re-run overwrites rather than
        # leaving an orphan; distinct per action.
        self.assertEqual(key, again)
        self.assertNotEqual(key, analyze_key)

    def test_presigns_put_for_that_one_key(self):
        scan = ScanFactory()
        s3 = _s3_stub()
        with patch("scanning.runpod_client.boto3.client", return_value=s3):
            key, _ = runpod_client._presign_result_put(scan, "analyze")

        # ContentType is signed, and must match the header the worker
        # sends byte for byte or S3 rejects the PUT.
        s3.generate_presigned_url.assert_called_once_with(
            "put_object",
            Params={
                "Bucket": "bucket",
                "Key": key,
                "ContentType": "application/json",
            },
            ExpiresIn=3600,
        )

    @override_settings(AWS_S3_REGION_NAME="us-west-2")
    def test_client_is_pinned_to_sigv4_and_the_bucket_region(self):
        # Both are ambient otherwise: SigV2 folds Content-Type into the
        # string to sign (which the worker's header then contradicts),
        # and an unset region signs for us-east-1, which S3 rejects
        # outright for a bucket that lives somewhere else.
        scan = ScanFactory()
        with patch(
            "scanning.runpod_client.boto3.client", return_value=_s3_stub()
        ) as mock_client:
            runpod_client._presign_result_put(scan, "detect")

        _, kwargs = mock_client.call_args
        self.assertEqual(kwargs["config"].signature_version, "s3v4")
        self.assertEqual(kwargs["region_name"], "us-west-2")


@override_settings(AWS_PRIVATE_STORAGE_BUCKET_NAME="bucket")
class TestResultObjectIsFresh(TestCase):
    """``_result_object_is_fresh`` gates on presence *and* write time."""

    KEY = "processing/1/x/1/1/jobs/detect/result.json"

    def _is_fresh(self, s3):
        with patch("scanning.runpod_client.boto3.client", return_value=s3):
            return runpod_client._result_object_is_fresh(
                self.KEY, _SUBMITTED_AT
            )

    def test_object_written_after_submit_is_ours(self):
        self.assertTrue(self._is_fresh(_s3_stub(last_modified=_FRESH)))

    def test_object_written_before_submit_is_a_leftover(self):
        # A previous attempt's output at the same key. Reading it would
        # report stale detections as this run's result.
        self.assertFalse(self._is_fresh(_s3_stub(last_modified=_STALE)))

    def test_clock_skew_is_tolerated(self):
        # S3's clock running slightly behind ours must not discard a
        # result the worker really did just write.
        just_before = _SUBMITTED_AT - timedelta(seconds=5)
        self.assertTrue(self._is_fresh(_s3_stub(last_modified=just_before)))

    def test_missing_object(self):
        self.assertFalse(
            self._is_fresh(_s3_stub(head_error=_client_error("404")))
        )

    def test_other_client_error_propagates(self):
        # AccessDenied means S3 is misconfigured, not that the worker
        # produced nothing.
        with self.assertRaises(ClientError):
            self._is_fresh(_s3_stub(head_error=_client_error("AccessDenied")))


@override_settings(AWS_PRIVATE_STORAGE_BUCKET_NAME="bucket")
class TestHarvest(TestCase):
    """``_harvest`` resolves an output into the action's payload."""

    def setUp(self):
        self.scan = ScanFactory()
        self.key = "processing/1/x/1/1/jobs/detect/result.json"

    def _harvest(self, output, envelope=None, s3=None):
        s3 = s3 if s3 is not None else _s3_stub(envelope=envelope)
        with patch("scanning.runpod_client.boto3.client", return_value=s3):
            return runpod_client.harvest(
                output, self.scan, "detect", self.key, _SUBMITTED_AT, _JOB_ID
            )

    def test_inline_output_passes_through(self):
        # No result_url was sent (or an older worker ignored it), so the
        # payload is already in the response.
        output = {"detections": [{"page_index": 0}], "duration_ms": 5}
        with patch("scanning.runpod_client.boto3.client") as mock_client:
            result = runpod_client.harvest(
                output, self.scan, "detect", None, _SUBMITTED_AT, _JOB_ID
            )
        self.assertEqual(result, output)
        mock_client.assert_not_called()

    def test_key_we_never_presigned_is_not_fetched(self):
        # Inline mode: we sent no result_url, so a response naming a key
        # is describing an object we didn't ask for.
        s3 = _s3_stub(envelope=_envelope(scan_pk=self.scan.pk))
        with patch("scanning.runpod_client.boto3.client", return_value=s3):
            with self.assertRaises(runpod_client.RunpodError):
                runpod_client.harvest(
                    {"result_key": "someone/elses.json"},
                    self.scan,
                    "detect",
                    None,
                    _SUBMITTED_AT,
                    _JOB_ID,
                )
        s3.get_object.assert_not_called()

    def test_s3_payload_is_merged_over_job_metadata(self):
        detections = [{"page_index": 0, "label": "CASE_CAPTION"}]
        envelope = _envelope(
            scan_pk=self.scan.pk, payload={"detections": detections}
        )
        output = {
            "result_key": self.key,
            "bytes": 120,
            "sha256": "abc",
            "status": "succeed",
            "duration_ms": 42,
        }
        result = self._harvest(output, envelope=envelope)
        self.assertEqual(result["detections"], detections)
        # Job-level timings survive the merge.
        self.assertEqual(result["duration_ms"], 42)
        self.assertEqual(result["bytes"], 120)

    def test_unexpected_key_is_terminal(self):
        envelope = _envelope(scan_pk=self.scan.pk)
        with self.assertRaises(runpod_client.RunpodError) as ctx:
            self._harvest(
                {"result_key": "somewhere/else.json"}, envelope=envelope
            )
        self.assertNotIsInstance(
            ctx.exception, runpod_client.RunpodTransientError
        )

    def test_unknown_schema_version_is_transient(self):
        # A worker image deployed ahead of the daemon. Failing the scan
        # outright would ERROR everything in flight over a deploy
        # ordering; re-queueing rides it out until the daemon catches up.
        envelope = _envelope(scan_pk=self.scan.pk, schema_version=99)
        with self.assertRaises(runpod_client.RunpodTransientError):
            self._harvest({"result_key": self.key}, envelope=envelope)

    def test_another_jobs_result_is_transient(self):
        # The authoritative staleness check: the key is reused across
        # runs, and a scan re-queued on the daemon's 5 s tick resubmits
        # well inside the clock-skew allowance, so LastModified alone
        # can call a leftover fresh. The job id can't.
        envelope = _envelope(scan_pk=self.scan.pk, job_id="an-earlier-job")
        with self.assertRaises(runpod_client.RunpodTransientError) as ctx:
            self._harvest({"result_key": self.key}, envelope=envelope)
        self.assertIn("earlier attempt", str(ctx.exception))

    def test_envelope_without_a_job_id_is_accepted(self):
        # Can't compare what isn't there; the freshness check stands.
        envelope = _envelope(scan_pk=self.scan.pk, job_id=None)
        result = self._harvest({"result_key": self.key}, envelope=envelope)
        self.assertEqual(result["detections"], [])

    def test_wrong_scan_pk_is_terminal(self):
        envelope = _envelope(scan_pk=self.scan.pk + 1000)
        with self.assertRaises(runpod_client.RunpodError):
            self._harvest({"result_key": self.key}, envelope=envelope)

    def test_wrong_action_is_terminal(self):
        envelope = _envelope(action="analyze", scan_pk=self.scan.pk)
        with self.assertRaises(runpod_client.RunpodError):
            self._harvest({"result_key": self.key}, envelope=envelope)

    def test_missing_payload_is_terminal(self):
        envelope = _envelope(scan_pk=self.scan.pk)
        del envelope["payload"]
        with self.assertRaises(runpod_client.RunpodError):
            self._harvest({"result_key": self.key}, envelope=envelope)

    def test_missing_object_is_transient(self):
        # The job said it succeeded but nothing is at the key: worth
        # re-running, and worth saying so plainly.
        s3 = _s3_stub(head_error=_client_error("404"))
        with self.assertRaises(runpod_client.RunpodTransientError) as ctx:
            self._harvest({"result_key": self.key}, s3=s3)
        self.assertIn("no result from this run", str(ctx.exception))
        s3.get_object.assert_not_called()

    def test_leftover_object_is_not_consumed(self):
        # Same key, but written before this job was submitted: it's a
        # previous attempt's output, not ours.
        s3 = _s3_stub(
            envelope=_envelope(scan_pk=self.scan.pk), last_modified=_STALE
        )
        with self.assertRaises(runpod_client.RunpodTransientError):
            self._harvest({"result_key": self.key}, s3=s3)
        s3.get_object.assert_not_called()

    def test_unreadable_object_is_transient(self):
        s3 = _s3_stub()
        s3.get_object.side_effect = _client_error("AccessDenied")
        with self.assertRaises(runpod_client.RunpodTransientError):
            self._harvest({"result_key": self.key}, s3=s3)

    def test_head_failure_is_transient_not_terminal(self):
        # Throttling, expired instance credentials, a region blip. The
        # result may be sitting there intact, so this must re-queue --
        # anything that isn't a RunpodTransientError ERRORs the scan.
        s3 = _s3_stub(head_error=_client_error("SlowDown"))
        with self.assertRaises(runpod_client.RunpodTransientError):
            self._harvest({"result_key": self.key}, s3=s3)

    def test_mid_read_connection_drop_is_transient(self):
        # ResponseStreamingError is a BotoCoreError, not a ClientError.
        s3 = _s3_stub()
        s3.get_object.side_effect = ResponseStreamingError(error="reset")
        with self.assertRaises(runpod_client.RunpodTransientError):
            self._harvest({"result_key": self.key}, s3=s3)

    def test_unparseable_body_is_transient(self):
        s3 = _s3_stub()
        s3.get_object.return_value = _body_stub(b"<html>nope</html>")
        with self.assertRaises(runpod_client.RunpodTransientError):
            self._harvest({"result_key": self.key}, s3=s3)

    def test_non_dict_envelope_is_terminal(self):
        s3 = _s3_stub()
        s3.get_object.return_value = _body_stub(b"[1, 2, 3]")
        with self.assertRaises(runpod_client.RunpodError) as ctx:
            self._harvest({"result_key": self.key}, s3=s3)
        self.assertNotIsInstance(
            ctx.exception, runpod_client.RunpodTransientError
        )


@override_settings(
    RUNPOD_ENABLED=True,
    RUNPOD_ENDPOINT_ID="ep-abc",
    RUNPOD_API_KEY="apikey",
    RUNPOD_REQUEST_TIMEOUT=900,
    RUNPOD_MAX_RETRIES=0,
    AWS_PRIVATE_STORAGE_BUCKET_NAME="bucket",
    RUNPOD_PRESIGNED_TTL=3600,
)
class TestInvokeWithResultsToS3(TestCase):
    """``_invoke`` submits a presigned PUT and harvests the object.

    ``_invoke`` stamps ``submitted_at`` from the real clock, so the
    stubbed objects here report a ``LastModified`` of "now" to look
    like something this run just wrote.
    """

    def setUp(self):
        self.scan = ScanFactory()
        patcher = patch(
            "scanning.runpod_client._results_to_s3_enabled",
            return_value=True,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _s3(self, envelope=None, **kwargs):
        """Stub S3 whose object was written just now, i.e. by this run."""
        kwargs.setdefault("last_modified", datetime.now(UTC))
        return _s3_stub(envelope=envelope, **kwargs)

    def _invoke(self, status_responses, s3, captured=None):
        """Run ``_invoke`` against stubbed RunPod HTTP and S3."""

        def fake_post(url, headers=None, json=None, timeout=None):
            if captured is not None:
                captured["body"] = json
            return _mock_response(200, {"id": "job-1"})

        with (
            patch("scanning.runpod_client.boto3.client", return_value=s3),
            patch(
                "scanning.runpod_client.requests.post", side_effect=fake_post
            ),
            patch(
                "scanning.runpod_client.requests.get",
                side_effect=[
                    _mock_response(c, b) for c, b in status_responses
                ],
            ),
            patch("scanning.runpod_client.time.sleep", return_value=None),
        ):
            return runpod_client._invoke(
                action="detect",
                scan=self.scan,
                payload={"pdf_url": "https://x/y.pdf"},
                progress_callback=None,
            )

    def test_job_input_carries_result_url_and_key(self):
        captured = {}
        s3 = self._s3(envelope=_envelope(scan_pk=self.scan.pk))
        # The worker echoes back the key it was given.
        with patch(
            "scanning.runpod_client._presign_result_put",
            return_value=("the/key.json", "https://signed.example/put"),
        ):
            self._invoke(
                [
                    (
                        200,
                        {
                            "status": "COMPLETED",
                            "output": {"result_key": "the/key.json"},
                        },
                    )
                ],
                s3,
                captured=captured,
            )
        job_input = captured["body"]["input"]
        self.assertEqual(job_input["result_key"], "the/key.json")
        self.assertEqual(job_input["result_url"], "https://signed.example/put")

    def test_completed_job_returns_payload_from_s3(self):
        detections = [{"page_index": 0, "label": "KEY_ICON"}]
        s3 = self._s3(
            envelope=_envelope(
                scan_pk=self.scan.pk, payload={"detections": detections}
            )
        )
        with patch(
            "scanning.runpod_client._presign_result_put",
            return_value=("the/key.json", "https://signed.example/put"),
        ):
            result = self._invoke(
                [
                    (
                        200,
                        {
                            "status": "COMPLETED",
                            "output": {
                                "status": "succeed",
                                "result_key": "the/key.json",
                                "bytes": 99,
                            },
                        },
                    )
                ],
                s3,
            )
        self.assertEqual(result["detections"], detections)

    def test_status_404_harvests_a_result_the_worker_already_wrote(self):
        # RunPod dropped the job record, but the worker had already
        # uploaded. head_object finds it, so the finished GPU run is
        # harvested instead of being paid for twice.
        detections = [{"page_index": 3}]
        s3 = self._s3(
            envelope=_envelope(
                scan_pk=self.scan.pk, payload={"detections": detections}
            )
        )
        with patch(
            "scanning.runpod_client._presign_result_put",
            return_value=("the/key.json", "https://signed.example/put"),
        ):
            result = self._invoke([(404, None)], s3)
        s3.head_object.assert_called_with(Bucket="bucket", Key="the/key.json")
        self.assertEqual(result["detections"], detections)

    def test_status_404_with_no_object_stays_transient(self):
        s3 = self._s3(head_error=_client_error("404"))
        with patch(
            "scanning.runpod_client._presign_result_put",
            return_value=("the/key.json", "https://signed.example/put"),
        ):
            with self.assertRaises(runpod_client.RunpodTransientError):
                self._invoke([(404, None)], s3)

    def test_status_404_with_s3_unreachable_stays_transient(self):
        # head_object failing must not be mistaken for a status-poll
        # blip: /status keeps 404ing, so retrying can only spin to the
        # deadline and then fail the scan terminally. Note AccessDenied
        # is what a *missing* key answers when the daemon's IAM lacks
        # s3:ListBucket, so this is a realistic shape, not a stretch.
        s3 = self._s3(head_error=_client_error("AccessDenied"))
        with patch(
            "scanning.runpod_client._presign_result_put",
            return_value=("the/key.json", "https://signed.example/put"),
        ):
            with self.assertRaises(runpod_client.RunpodTransientError):
                self._invoke([(404, None)], s3)
        # One probe, one answer: no retry loop.
        self.assertEqual(s3.head_object.call_count, 1)

    def test_status_404_with_a_leftover_object_stays_transient(self):
        # An object is there, but it predates this submission: it's the
        # previous attempt's output, so resubmit rather than consume it.
        s3 = self._s3(
            envelope=_envelope(scan_pk=self.scan.pk),
            last_modified=datetime.now(UTC) - timedelta(hours=2),
        )
        with patch(
            "scanning.runpod_client._presign_result_put",
            return_value=("the/key.json", "https://signed.example/put"),
        ):
            with self.assertRaises(runpod_client.RunpodTransientError):
                self._invoke([(404, None)], s3)
        s3.get_object.assert_not_called()

    def test_worker_answering_inline_is_still_accepted(self):
        # An older worker image ignores result_url and returns the
        # payload in the response. Use it rather than failing.
        output = {"detections": [{"page_index": 1}], "duration_ms": 3}
        s3 = self._s3()
        with patch(
            "scanning.runpod_client._presign_result_put",
            return_value=("the/key.json", "https://signed.example/put"),
        ):
            result = self._invoke(
                [(200, {"status": "COMPLETED", "output": output})], s3
            )
        self.assertEqual(result, output)
        s3.get_object.assert_not_called()


# ── reusable_result ─────────────────────────────────────────────────
@override_settings(
    RUNPOD_ENABLED=True,
    AWS_PRIVATE_STORAGE_BUCKET_NAME="bucket",
)
class TestReusableResult(TestCase):
    """Reusing a finished job's result when the input hasn't changed.

    The resume path for a daemon killed mid-pipeline. Every negative
    answer here has the same consequence -- the caller runs the stage --
    so the bar is that nothing raises and nothing accepts an object that
    predates the current input.
    """

    def setUp(self):
        self.scan = ScanFactory()
        self.input_at = datetime.now(UTC) - timedelta(hours=1)

    def _s3(self, result_at, envelope=None, input_at=None):
        """Build an S3 client whose two heads answer result then input.

        :param result_at: ``LastModified`` for the result object.
        :param envelope: Envelope ``get_object`` should return.
        :param input_at: ``LastModified`` for the input PDF.
        :returns: The mock client.
        :rtype: MagicMock
        """
        s3 = MagicMock()
        s3.head_object.side_effect = [
            {"LastModified": result_at},
            {"LastModified": input_at or self.input_at},
        ]
        if envelope is not None:
            s3.get_object.return_value = _body_stub(
                json.dumps(envelope).encode()
            )
        return s3

    def _call(self, s3, action="detect", creds=True):
        """Invoke ``reusable_result`` against a mocked S3."""
        with (
            patch("scanning.runpod_client._s3", return_value=s3),
            patch("scanning.utils.has_s3_credentials", return_value=creds),
        ):
            return runpod_client.reusable_result(
                self.scan, action, "bitonal.pdf"
            )

    def test_result_newer_than_input_is_reused(self):
        envelope = _envelope(
            scan_pk=self.scan.pk,
            payload={"detections": [{"page_index": 1}]},
        )
        s3 = self._s3(datetime.now(UTC), envelope=envelope)
        self.assertEqual(self._call(s3), {"detections": [{"page_index": 1}]})

    def test_heads_the_result_key_then_the_input_key(self):
        # Both keys are built here, and every other test in this class
        # stubs head_object positionally -- so without this one, a key
        # pointing outside the scan's processing prefix would go unnoticed.
        from scanning import s3_sync

        envelope = _envelope(scan_pk=self.scan.pk)
        s3 = self._s3(datetime.now(UTC), envelope=envelope)
        self._call(s3)

        prefix = s3_sync.s3_processing_prefix(self.scan)
        self.assertEqual(
            [c.kwargs["Key"] for c in s3.head_object.call_args_list],
            [f"{prefix}jobs/detect/result.json", f"{prefix}bitonal.pdf"],
        )
        self.assertEqual(
            s3.get_object.call_args.kwargs["Key"],
            f"{prefix}jobs/detect/result.json",
        )

    def test_analyze_heads_the_original_pdf_as_its_input(self):
        # The analyze stage runs against the original, not the bitonal, so
        # its freshness reference is a different object.
        from scanning import s3_sync

        envelope = _envelope(action="analyze", scan_pk=self.scan.pk)
        s3 = self._s3(datetime.now(UTC), envelope=envelope)
        with (
            patch("scanning.runpod_client._s3", return_value=s3),
            patch("scanning.utils.has_s3_credentials", return_value=True),
        ):
            runpod_client.reusable_result(
                self.scan, "analyze", "a3d.1.1.2.original.pdf"
            )

        prefix = s3_sync.s3_processing_prefix(self.scan)
        self.assertEqual(
            [c.kwargs["Key"] for c in s3.head_object.call_args_list],
            [
                f"{prefix}jobs/analyze/result.json",
                f"{prefix}a3d.1.1.2.original.pdf",
            ],
        )

    def test_result_older_than_input_is_rejected(self):
        # The page-editing paths rewrite bitonal.pdf and re-upload it, so
        # a result from before that edit describes pages that no longer
        # exist. Re-run rather than reuse.
        s3 = self._s3(self.input_at - timedelta(minutes=5))
        self.assertIsNone(self._call(s3))
        s3.get_object.assert_not_called()

    def test_foreign_job_id_is_still_reusable(self):
        # The deliberate inversion of _validate_envelope: we are knowingly
        # reading the *previous* run's object, so its job_id never matches
        # and must not disqualify it.
        envelope = _envelope(
            scan_pk=self.scan.pk,
            payload={"detections": []},
            job_id="some-earlier-job",
        )
        s3 = self._s3(datetime.now(UTC), envelope=envelope)
        self.assertEqual(self._call(s3), {"detections": []})

    def test_envelope_for_another_scan_is_rejected(self):
        envelope = _envelope(scan_pk=self.scan.pk + 1000)
        s3 = self._s3(datetime.now(UTC), envelope=envelope)
        self.assertIsNone(self._call(s3))

    def test_envelope_for_another_action_is_rejected(self):
        envelope = _envelope(action="analyze", scan_pk=self.scan.pk)
        s3 = self._s3(datetime.now(UTC), envelope=envelope)
        self.assertIsNone(self._call(s3))

    def test_future_schema_version_is_rejected(self):
        envelope = _envelope(
            scan_pk=self.scan.pk,
            schema_version=runpod_client.RESULT_SCHEMA_VERSION + 1,
        )
        s3 = self._s3(datetime.now(UTC), envelope=envelope)
        self.assertIsNone(self._call(s3))

    def test_missing_result_object_is_not_an_error(self):
        s3 = MagicMock()
        s3.head_object.side_effect = _client_error("404")
        self.assertIsNone(self._call(s3))

    def test_unparseable_body_is_swallowed(self):
        s3 = self._s3(datetime.now(UTC))
        s3.get_object.return_value = _body_stub(b"not json")
        self.assertIsNone(self._call(s3))

    def test_payload_missing_is_rejected(self):
        envelope = _envelope(scan_pk=self.scan.pk)
        del envelope["payload"]
        s3 = self._s3(datetime.now(UTC), envelope=envelope)
        self.assertIsNone(self._call(s3))

    @override_settings(RUNPOD_ENABLED=False)
    def test_local_mode_never_heads(self):
        # Local mode writes no result object, so the two round trips
        # would only ever confirm there is nothing there.
        s3 = MagicMock()
        self.assertIsNone(self._call(s3))
        s3.head_object.assert_not_called()

    def test_missing_credentials_never_heads(self):
        s3 = MagicMock()
        self.assertIsNone(self._call(s3, creds=False))
        s3.head_object.assert_not_called()


# ── Non-blocking submit + single-shot poll ──────────────────────────
@override_settings(
    RUNPOD_ENABLED=True,
    RUNPOD_ENDPOINT_ID="ep-abc",
    RUNPOD_API_KEY="apikey",
    RUNPOD_MAX_RETRIES=0,
    RUNPOD_PRESIGNED_TTL=3600,
    AWS_PRIVATE_STORAGE_BUCKET_NAME="bucket",
)
class TestSubmitJob(TestCase):
    """``submit_job`` hands work over and returns without waiting."""

    def _submit(self, s3=None, **kwargs):
        """Submit against a stubbed ``/run`` and return the receipt and body.

        :param s3: Stub S3 client, or None to run in inline mode.
        :param kwargs: Extra arguments for ``submit_job``.
        :returns: ``(receipt, submitted_body)``.
        :rtype: tuple
        """
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["body"] = json
            return _mock_response(200, {"id": "job-xyz"})

        with (
            patch(
                "scanning.runpod_client.requests.post", side_effect=fake_post
            ),
            patch(
                "scanning.runpod_client._results_to_s3_enabled",
                return_value=s3 is not None,
            ),
            patch(
                "scanning.runpod_client.boto3.client",
                return_value=s3 or MagicMock(),
            ),
        ):
            receipt = runpod_client.submit_job(
                action="detect",
                scan=self.scan,
                payload={"pdf_url": "https://x/y.pdf"},
                **kwargs,
            )
        return receipt, captured["body"]

    def setUp(self):
        self.scan = ScanFactory()

    def test_returns_the_job_id_without_polling(self):
        with patch("scanning.runpod_client.requests.get") as mock_get:
            receipt, _ = self._submit()
        self.assertEqual(receipt.external_id, "job-xyz")
        # The whole point: nothing waits on the job it just submitted.
        mock_get.assert_not_called()

    def test_receipt_timestamp_precedes_the_result_it_will_match(self):
        # Taken before the POST so a slow /run can only widen the window
        # a result object is allowed to land in, never miss one.
        before = datetime.now(UTC)
        receipt, _ = self._submit()
        self.assertLessEqual(before, receipt.submitted_at)
        self.assertLessEqual(receipt.submitted_at, datetime.now(UTC))

    def test_inline_mode_presigns_nothing(self):
        receipt, body = self._submit()
        self.assertEqual(receipt.result_key, "")
        self.assertNotIn("result_url", body["input"])
        self.assertNotIn("result_key", body["input"])

    def test_default_key_is_used_when_the_caller_names_none(self):
        from scanning import s3_sync

        receipt, body = self._submit(s3=_s3_stub())
        expected = s3_sync.s3_job_result_key(self.scan, "detect")
        self.assertEqual(receipt.result_key, expected)
        self.assertEqual(body["input"]["result_key"], expected)

    def test_callers_key_wins_so_concurrent_jobs_do_not_collide(self):
        # Two live jobs for one scan (shards of a pass, or a retry
        # racing the attempt it replaced) would otherwise presign the
        # same object and the survivor would be whichever wrote last.
        key = "processing/9/a/1/1/jobs/detect/run1-shard2-attempt1.json"
        s3 = _s3_stub()
        receipt, body = self._submit(s3=s3, result_key=key)

        self.assertEqual(receipt.result_key, key)
        self.assertEqual(body["input"]["result_key"], key)
        _, kwargs = s3.generate_presigned_url.call_args
        self.assertEqual(kwargs["Params"]["Key"], key)

    @override_settings(RUNPOD_ENDPOINT_ID="")
    def test_unconfigured_endpoint_raises(self):
        with self.assertRaises(runpod_client.RunpodError):
            runpod_client.submit_job(
                action="detect", scan=self.scan, payload={}
            )


@override_settings(AWS_PRIVATE_STORAGE_BUCKET_NAME="bucket")
class TestPollOnce(TestCase):
    """``poll_once`` normalizes one ``/status`` answer onto JobStatus."""

    KEY = "processing/1/x/1/1/jobs/detect/result.json"

    def _poll_once(self, status_code, body, s3=None, **kwargs):
        """Run ``poll_once`` against one stubbed ``/status`` response.

        :param status_code: HTTP status the poll receives.
        :param body: Parsed JSON body, or None for an unparseable one.
        :param s3: Stub S3 client for the 404 salvage probe.
        :param kwargs: Extra arguments for ``poll_once``.
        :returns: The outcome.
        :rtype: runpod_client.PollOutcome
        """
        with (
            patch(
                "scanning.runpod_client.requests.get",
                return_value=_mock_response(status_code, body),
            ),
            patch(
                "scanning.runpod_client.boto3.client",
                return_value=s3 or MagicMock(),
            ),
        ):
            return runpod_client.poll_once(
                base_url="https://api/run/endpoint",
                headers={},
                job_id=_JOB_ID,
                action="detect",
                **kwargs,
            )

    def test_never_sleeps(self):
        # Pacing belongs to the daemon's tick, not to one job's poll.
        # A sleep in here would serialize the batch it is meant to
        # let run at once.
        with patch("scanning.runpod_client.time.sleep") as mock_sleep:
            self._poll_once(200, {"status": "IN_QUEUE"})
        mock_sleep.assert_not_called()

    def test_in_queue(self):
        outcome = self._poll_once(200, {"status": "IN_QUEUE"})
        self.assertEqual(outcome.status, JobStatus.IN_QUEUE)
        self.assertEqual(outcome.provider_status, "IN_QUEUE")
        self.assertIsNone(outcome.output)

    def test_in_progress(self):
        outcome = self._poll_once(200, {"status": "IN_PROGRESS"})
        self.assertEqual(outcome.status, JobStatus.IN_PROGRESS)

    def test_unrecognised_status_reads_as_still_working(self):
        # RunPod adding a state must not fail every job that is in it.
        outcome = self._poll_once(200, {"status": "SOMETHING_NEW"})
        self.assertEqual(outcome.status, JobStatus.IN_PROGRESS)
        self.assertEqual(outcome.provider_status, "SOMETHING_NEW")

    def test_completed_carries_the_output(self):
        output = {"detections": [], "duration_ms": 12}
        outcome = self._poll_once(
            200, {"status": "COMPLETED", "output": output}
        )
        self.assertEqual(outcome.status, JobStatus.COMPLETED)
        self.assertEqual(outcome.output, output)

    def test_completed_with_unusable_output_is_terminal(self):
        # Success with a body we cannot read. Resubmitting would
        # produce the same shape, so it does not get a retry.
        outcome = self._poll_once(
            200, {"status": "COMPLETED", "output": "a string"}
        )
        self.assertEqual(outcome.status, JobStatus.FAILED)
        self.assertEqual(outcome.error_code, "BAD_OUTPUT")
        self.assertFalse(outcome.retriable)

    def test_handler_error_codes_are_classified_by_cause(self):
        cases = {
            "NO_GPU": True,
            "RESULT_UPLOAD_FAILED": True,
            "RESULT_URL_EXPIRED": True,
            "RESULT_UPLOAD_REJECTED": False,
            "BAD_INPUT": False,
        }
        for code, retriable in cases.items():
            with self.subTest(code=code):
                outcome = self._poll_once(
                    200,
                    {
                        "status": "FAILED",
                        "error": "boom",
                        "output": {"error_code": code},
                    },
                )
                self.assertEqual(outcome.status, JobStatus.FAILED)
                self.assertEqual(outcome.error_code, code)
                self.assertEqual(outcome.retriable, retriable)

    def test_failed_with_no_output_is_a_platform_failure(self):
        # No error_code means RunPod itself failed the job (worker
        # crash, internal retries exhausted), which another submit can
        # plausibly get past.
        outcome = self._poll_once(
            200, {"status": "FAILED", "error": "job timed out after 1 retries"}
        )
        self.assertEqual(outcome.status, JobStatus.FAILED)
        self.assertTrue(outcome.retriable)

    def test_timed_out_and_cancelled_are_not_retriable(self):
        for status, expected in (
            ("TIMED_OUT", JobStatus.FAILED),
            ("CANCELLED", JobStatus.CANCELLED),
        ):
            with self.subTest(status=status):
                outcome = self._poll_once(
                    200, {"status": status, "error": "boom"}
                )
                self.assertEqual(outcome.status, expected)
                self.assertFalse(outcome.retriable)

    def test_lost_job_with_a_result_on_s3_still_completes(self):
        # The job record aged out but the worker's PUT outlived it.
        outcome = self._poll_once(
            404,
            None,
            s3=_s3_stub(last_modified=_FRESH),
            result_key=self.KEY,
            submitted_at=_SUBMITTED_AT,
        )
        self.assertEqual(outcome.status, JobStatus.COMPLETED)
        self.assertEqual(outcome.output, {"result_key": self.KEY})

    def test_lost_job_with_nothing_on_s3_expires(self):
        outcome = self._poll_once(
            404,
            None,
            s3=_s3_stub(head_error=_client_error("404")),
            result_key=self.KEY,
            submitted_at=_SUBMITTED_AT,
        )
        self.assertEqual(outcome.status, JobStatus.EXPIRED)
        self.assertEqual(outcome.error_code, "JOB_NOT_FOUND")
        # The inputs are still on S3, so a fresh submit re-runs the work.
        self.assertTrue(outcome.retriable)

    def test_stale_object_does_not_rescue_a_lost_job(self):
        # An earlier attempt's leftover at the same key is not this
        # attempt's output.
        outcome = self._poll_once(
            404,
            None,
            s3=_s3_stub(last_modified=_STALE),
            result_key=self.KEY,
            submitted_at=_SUBMITTED_AT,
        )
        self.assertEqual(outcome.status, JobStatus.EXPIRED)

    def test_a_failed_poll_reports_no_status_rather_than_a_failure(self):
        # We learned nothing about the job, which is not the same as
        # learning it failed. The caller must leave the row alone.
        with patch(
            "scanning.runpod_client.requests.get",
            side_effect=requests.ConnectionError("refused"),
        ):
            outcome = runpod_client.poll_once(
                base_url="https://api/run/endpoint",
                headers={},
                job_id=_JOB_ID,
                action="detect",
            )
        self.assertIsNone(outcome.status)
        self.assertIn("refused", outcome.error_message)

    def test_a_5xx_reports_no_status(self):
        outcome = self._poll_once(503, {"error": "upstream"})
        self.assertIsNone(outcome.status)
