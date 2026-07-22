"""Tests for ``scanning.runpod_client``.

All RunPod HTTP traffic is mocked at the ``requests`` layer; nothing
here hits the network or the RunPod API. Local-fallback paths mock
blackletter's entry points so the tests don't need GPU or large
PDFs.

Covers:

- ``_redact_pdf_url`` masking behaviour.
- ``_ensure_presigned_url`` upload / head-object / error classification.
- ``_submit`` retry logic and malformed-response handling.
- ``_poll`` terminal states, transient classification, deadline expiry,
  404-as-transient, and structured-error mapping.
- ``_invoke`` body construction and missing-credential guard.
- Public ``detect`` / ``analyze`` local fallback + remote dispatch.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests
from botocore.exceptions import ClientError
from django.test import TestCase, override_settings
from django.utils import timezone

from scanning import runpod_client
from scanning.factories import ScanFactory
from scanning.models import Scan


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
class TestRedactPdfUrl(TestCase):
    """``_redact_pdf_url`` masks pdf_url without mutating input."""

    def test_masks_pdf_url_under_input(self):
        body = {
            "input": {
                "action": "detect",
                "scan_pk": 42,
                "pdf_url": "https://s3.example/secret?X-Amz-Signature=abc",
                "models": ["small"],
            }
        }
        redacted = runpod_client._redact_pdf_url(body)
        self.assertEqual(redacted["input"]["pdf_url"], "***")

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
        redacted = runpod_client._redact_pdf_url(body)
        self.assertEqual(redacted["input"]["action"], "detect")
        self.assertEqual(redacted["input"]["scan_pk"], 42)
        self.assertEqual(redacted["input"]["models"], ["small"])
        self.assertEqual(redacted["input"]["confidence"], 0.2)

    def test_does_not_mutate_original(self):
        body = {"input": {"pdf_url": "https://s3.example/x"}}
        runpod_client._redact_pdf_url(body)
        self.assertEqual(body["input"]["pdf_url"], "https://s3.example/x")

    def test_no_pdf_url_no_change(self):
        body = {"input": {"action": "detect", "scan_pk": 42}}
        redacted = runpod_client._redact_pdf_url(body)
        self.assertNotIn("pdf_url", redacted["input"])

    def test_non_dict_input_not_touched(self):
        body = {"input": "not a dict"}
        redacted = runpod_client._redact_pdf_url(body)
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
            with self.assertRaises(runpod_client.RunpodError):
                runpod_client._submit(
                    base_url="https://api/run/endpoint",
                    headers={},
                    body={"input": {}},
                    action="detect",
                    max_retries=1,
                    progress_callback=None,
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
    """``_invoke`` body shape and missing-config guards."""

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
    """``detect()`` with RUNPOD_ENABLED=True walks presign + submit + poll."""

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
    """``analyze()`` with RUNPOD_ENABLED=True walks presign + submit + poll."""

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


# ── Persist / clear job id (issue #127) ─────────────────────────────
@override_settings(
    RUNPOD_ENABLED=True,
    RUNPOD_ENDPOINT_ID="ep-abc",
    RUNPOD_API_KEY="apikey",
    RUNPOD_REQUEST_TIMEOUT=900,
    RUNPOD_MAX_RETRIES=0,
)
class TestPersistAndClearJobId(TestCase):
    """The in-flight RunPod job id is persisted on submit and cleared on
    any terminal outcome, so a killed daemon leaves the id behind to reattach."""

    def test_persist_helper_sets_fields(self):
        scan = ScanFactory()
        runpod_client._persist_job_id(scan.pk, "job-1", "detect")
        scan.refresh_from_db()
        self.assertEqual(scan.runpod_job_id, "job-1")
        self.assertEqual(scan.runpod_job_action, "detect")
        self.assertIsNotNone(scan.runpod_job_submitted_at)

    def test_clear_helper_only_clears_matching_job(self):
        scan = ScanFactory(
            runpod_job_id="job-1",
            runpod_job_action="detect",
            runpod_job_submitted_at=timezone.now(),
        )
        # Guard: a stale clear for a different id must not wipe the row.
        runpod_client._clear_job_id(scan.pk, "other")
        scan.refresh_from_db()
        self.assertEqual(scan.runpod_job_id, "job-1")

        runpod_client._clear_job_id(scan.pk, "job-1")
        scan.refresh_from_db()
        self.assertEqual(scan.runpod_job_id, "")
        self.assertEqual(scan.runpod_job_action, "")
        self.assertIsNone(scan.runpod_job_submitted_at)

    def test_invoke_persists_before_poll_then_clears(self):
        scan = ScanFactory()
        seen = {}

        def fake_poll(
            base_url, headers, job_id, action, deadline, progress_callback
        ):
            # Snapshot what is persisted while the job is "in flight".
            row = Scan.objects.get(pk=scan.pk)
            seen["job_id"] = row.runpod_job_id
            seen["action"] = row.runpod_job_action
            return {"detections": []}

        with (
            patch(
                "scanning.runpod_client.requests.post",
                return_value=_mock_response(200, {"id": "job-xyz"}),
            ),
            patch("scanning.runpod_client._poll", side_effect=fake_poll),
            patch("scanning.runpod_client.time.sleep", return_value=None),
        ):
            runpod_client._invoke(
                action="detect",
                scan=scan,
                payload={"pdf_url": "x"},
                progress_callback=None,
            )

        # Persisted before poll ran...
        self.assertEqual(seen["job_id"], "job-xyz")
        self.assertEqual(seen["action"], "detect")
        # ...and cleared in the finally once poll returned.
        scan.refresh_from_db()
        self.assertEqual(scan.runpod_job_id, "")

    def test_invoke_clears_on_terminal_error(self):
        scan = ScanFactory()
        with (
            patch(
                "scanning.runpod_client.requests.post",
                return_value=_mock_response(200, {"id": "job-err"}),
            ),
            patch(
                "scanning.runpod_client.requests.get",
                return_value=_mock_response(200, {"status": "TIMED_OUT"}),
            ),
            patch("scanning.runpod_client.time.sleep", return_value=None),
        ):
            with self.assertRaises(runpod_client.RunpodError):
                runpod_client._invoke(
                    action="detect",
                    scan=scan,
                    payload={"pdf_url": "x"},
                    progress_callback=None,
                )
        scan.refresh_from_db()
        self.assertEqual(scan.runpod_job_id, "")


# ── Reattach vs fresh submit (issue #127) ───────────────────────────
@override_settings(
    RUNPOD_ENABLED=True,
    RUNPOD_ENDPOINT_ID="ep-abc",
    RUNPOD_API_KEY="apikey",
    RUNPOD_REQUEST_TIMEOUT=900,
    RUNPOD_MAX_RETRIES=0,
)
class TestResumeOrSubmit(TestCase):
    """``_invoke`` reattaches to a persisted in-flight job when one is fresh,
    otherwise submits a new one."""

    _OUTPUT = {"detections": [], "duration_ms": 1}

    def _run(self, scan, get_responses):
        with (
            patch(
                "scanning.runpod_client.requests.post",
                return_value=_mock_response(200, {"id": "fresh-job"}),
            ) as mock_post,
            patch(
                "scanning.runpod_client.requests.get",
                side_effect=get_responses,
            ),
            patch("scanning.runpod_client.time.sleep", return_value=None),
        ):
            result = runpod_client._invoke(
                action="detect",
                scan=scan,
                payload={"pdf_url": "x"},
                progress_callback=None,
            )
        return result, mock_post

    @staticmethod
    def _completed():
        return _mock_response(
            200, {"status": "COMPLETED", "output": TestResumeOrSubmit._OUTPUT}
        )

    def _assert_submitted_fresh(self, mock_post):
        self.assertTrue(mock_post.called)
        urls = [c.args[0] for c in mock_post.call_args_list]
        self.assertTrue(any(u.endswith("/run") for u in urls))

    def test_no_persisted_id_submits_fresh(self):
        scan = ScanFactory()
        result, mock_post = self._run(scan, [self._completed()])
        self.assertEqual(result, self._OUTPUT)
        self._assert_submitted_fresh(mock_post)

    def test_reattaches_to_completed_job_without_submitting(self):
        scan = ScanFactory(
            runpod_job_id="job-old",
            runpod_job_action="detect",
            runpod_job_submitted_at=timezone.now(),
        )
        result, mock_post = self._run(scan, [self._completed()])
        self.assertEqual(result, self._OUTPUT)
        mock_post.assert_not_called()  # no duplicate /run

    def test_reattaches_in_progress_then_completed(self):
        scan = ScanFactory(
            runpod_job_id="job-old",
            runpod_job_action="detect",
            runpod_job_submitted_at=timezone.now(),
        )
        result, mock_post = self._run(
            scan,
            [
                _mock_response(200, {"status": "IN_PROGRESS"}),
                self._completed(),
            ],
        )
        self.assertEqual(result, self._OUTPUT)
        mock_post.assert_not_called()

    def test_wrong_action_submits_fresh(self):
        scan = ScanFactory(
            runpod_job_id="job-old",
            runpod_job_action="analyze",  # different stage
            runpod_job_submitted_at=timezone.now(),
        )
        result, mock_post = self._run(scan, [self._completed()])
        self.assertEqual(result, self._OUTPUT)
        self._assert_submitted_fresh(mock_post)

    def test_past_window_submits_fresh(self):
        scan = ScanFactory(
            runpod_job_id="job-old",
            runpod_job_action="detect",
            runpod_job_submitted_at=timezone.now() - timedelta(hours=25),
        )
        result, mock_post = self._run(scan, [self._completed()])
        self.assertEqual(result, self._OUTPUT)
        self._assert_submitted_fresh(mock_post)

    def test_404_on_reattach_raises_transient_and_clears(self):
        scan = ScanFactory(
            runpod_job_id="job-old",
            runpod_job_action="detect",
            runpod_job_submitted_at=timezone.now(),
        )
        with (
            patch(
                "scanning.runpod_client.requests.post",
                return_value=_mock_response(200, {"id": "unused"}),
            ) as mock_post,
            patch(
                "scanning.runpod_client.requests.get",
                return_value=_mock_response(404, None),
            ),
            patch("scanning.runpod_client.time.sleep", return_value=None),
        ):
            with self.assertRaises(runpod_client.RunpodTransientError):
                runpod_client._invoke(
                    action="detect",
                    scan=scan,
                    payload={"pdf_url": "x"},
                    progress_callback=None,
                )
        mock_post.assert_not_called()  # reattached, never submitted
        scan.refresh_from_db()
        self.assertEqual(scan.runpod_job_id, "")  # cleared for fresh next tick


# ── cancel_job wrapper (issue #127) ─────────────────────────────────
class TestCancelJob(TestCase):
    """``cancel_job`` wires the endpoint from settings and no-ops when
    RunPod is not configured."""

    @override_settings(
        RUNPOD_ENABLED=True,
        RUNPOD_ENDPOINT_ID="ep-abc",
        RUNPOD_API_KEY="apikey",
    )
    def test_posts_to_cancel(self):
        with patch(
            "scanning.runpod_client.requests.post",
            return_value=_mock_response(200, {}),
        ) as mock_post:
            runpod_client.cancel_job("job-9")
        self.assertTrue(mock_post.called)
        self.assertIn("/cancel/job-9", mock_post.call_args.args[0])

    @override_settings(RUNPOD_ENDPOINT_ID="", RUNPOD_API_KEY="")
    def test_noop_without_config(self):
        with patch("scanning.runpod_client.requests.post") as mock_post:
            runpod_client.cancel_job("job-9")
        mock_post.assert_not_called()

    @override_settings(
        RUNPOD_ENABLED=True,
        RUNPOD_ENDPOINT_ID="ep-abc",
        RUNPOD_API_KEY="apikey",
    )
    def test_noop_empty_job_id(self):
        with patch("scanning.runpod_client.requests.post") as mock_post:
            runpod_client.cancel_job("")
        mock_post.assert_not_called()


# ── Page-aware request timeout (issue #127) ─────────────────────────
class TestRequestTimeoutForScan(TestCase):
    """The wall-clock ceiling scales with page count so large volumes are
    not cut off against a base tuned for small scans."""

    @override_settings(
        RUNPOD_REQUEST_TIMEOUT=1800, RUNPOD_REQUEST_TIMEOUT_PER_PAGE=2
    )
    def test_scales_with_page_count(self):
        scan = ScanFactory(page_count=1303)
        self.assertEqual(
            runpod_client._request_timeout_for(scan), 1800 + 2 * 1303
        )

    @override_settings(
        RUNPOD_REQUEST_TIMEOUT=1800, RUNPOD_REQUEST_TIMEOUT_PER_PAGE=2
    )
    def test_falls_back_to_base_without_page_count(self):
        scan = ScanFactory(page_count=0)
        self.assertEqual(runpod_client._request_timeout_for(scan), 1800)
