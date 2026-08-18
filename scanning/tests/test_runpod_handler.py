"""Tests for the standalone RunPod GPU-worker handler.

``scanning/runpod/handler.py`` is a separate deploy artifact: only that
one file is copied into the worker image, and it imports the GPU stack
(``runpod``/``torch``/``blackletter``) at module scope and calls
``_preload()`` on import. None of that is installed in the scanning test
environment, so :func:`_load_handler` injects lightweight stubs into
``sys.modules`` before importing the module from its file path. The
torch stub reports no CUDA, which makes ``_preload()`` return early
before it touches any real model code.

The handler imports the shared transfer code as a top-level
``runpod_common`` module (the Dockerfile copies it next to handler.py),
so the loader aliases the real ``scanning.runpod_common`` under that
name. Its download/validation behaviour is covered in
``test_runpod_common.py``; these cover what is handler-specific: the
result-delivery path (``requests.put`` standing in for the presigned
PUT target) and the dispatch surface.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from unittest import mock

import requests
from django.test import SimpleTestCase

from scanning import runpod_common

_HANDLER_PATH = (
    Path(__file__).resolve().parent.parent / "runpod" / "handler.py"
)


def _load_handler():
    """Import the handler module with its GPU-only deps stubbed."""
    stub_torch = mock.MagicMock()
    stub_torch.cuda.is_available.return_value = False
    stubs = {
        "runpod": mock.MagicMock(),
        "torch": stub_torch,
        "blackletter": mock.MagicMock(),
        "blackletter.api": mock.MagicMock(),
        # The real shared module, under the top-level name the worker
        # image gives it.
        "runpod_common": runpod_common,
    }
    with mock.patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location(
            "_runpod_handler_under_test", _HANDLER_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


handler = _load_handler()


class PutResultTest(SimpleTestCase):
    """``handler._put_result`` upload retries and error classification."""

    URL = "https://s3.example/presigned/result.json?X-Amz-Signature=abc"

    def setUp(self):
        mock.patch.object(handler.time, "sleep").start()
        mock.patch.object(handler.random, "uniform", return_value=0.0).start()
        self.addCleanup(mock.patch.stopall)

    def _patch_put(self, outcomes):
        """Patch ``requests.put`` to yield ``outcomes`` in order.

        Each outcome is either an HTTP status code or an exception
        instance to raise instead of responding.
        """
        calls = []
        it = iter(outcomes)

        def fake_put(url, data=None, headers=None, timeout=None):
            calls.append({"url": url, "data": data, "headers": headers or {}})
            outcome = next(it)
            if isinstance(outcome, Exception):
                raise outcome
            # An outcome may be a bare status, or (status, body) when
            # the test cares what S3 said.
            status, text = (
                outcome
                if isinstance(outcome, tuple)
                else (outcome, f"HTTP {outcome}")
            )
            resp = mock.MagicMock()
            resp.status_code = status
            resp.text = text
            return resp

        patcher = mock.patch.object(
            handler.requests, "put", side_effect=fake_put
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_single_put_on_success(self):
        calls = self._patch_put([200])
        handler._put_result(self.URL, b'{"a":1}')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["url"], self.URL)
        self.assertEqual(calls[0]["data"], b'{"a":1}')
        # Signed into the presigned URL daemon-side, so this must match
        # what _presign_result_put signs or S3 rejects the PUT.
        self.assertEqual(
            calls[0]["headers"].get("Content-Type"),
            handler.RESULT_CONTENT_TYPE,
        )

    def test_retries_5xx_then_succeeds(self):
        # The GPU work is already paid for, so a server-side blip must
        # not cost a re-run.
        calls = self._patch_put([503, 500, 200])
        handler._put_result(self.URL, b"{}")
        self.assertEqual(len(calls), 3)

    def test_retries_connection_error_then_succeeds(self):
        calls = self._patch_put(
            [requests.exceptions.ConnectionError("reset"), 200]
        )
        handler._put_result(self.URL, b"{}")
        self.assertEqual(len(calls), 2)

    def test_403_fails_fast_as_expired(self):
        # Retrying a dead signature can never succeed.
        calls = self._patch_put([403, 200])
        with self.assertRaises(handler.ResultUrlExpiredError) as ctx:
            handler._put_result(self.URL, b"{}")
        self.assertEqual(len(calls), 1)
        self.assertEqual(ctx.exception.error_code, "RESULT_URL_EXPIRED")

    def test_other_4xx_is_terminal_and_not_retried(self):
        # e.g. a signature scoped to the wrong region. Identical on
        # every retry, and each one costs another GPU run, so this
        # reports a code the daemon does NOT classify as transient.
        calls = self._patch_put([400, 200])
        with self.assertRaises(handler.ResultUploadRejectedError) as ctx:
            handler._put_result(self.URL, b"{}")
        self.assertEqual(len(calls), 1)
        self.assertEqual(ctx.exception.error_code, "RESULT_UPLOAD_REJECTED")

    def test_rejection_body_is_surfaced(self):
        # S3's XML says *why* (wrong region, missing encryption header).
        # Losing it turns a five-second fix into a debugging session.
        self._patch_put(
            [(400, "<Error><Code>AuthorizationQueryParametersError</Code>")]
        )
        with self.assertRaisesRegex(
            handler.ResultUploadError, "AuthorizationQueryParametersError"
        ):
            handler._put_result(self.URL, b"{}")

    def test_gives_up_after_max_attempts(self):
        calls = self._patch_put([503] * handler.RESULT_UPLOAD_MAX_ATTEMPTS)
        with self.assertRaises(handler.ResultUploadError) as ctx:
            handler._put_result(self.URL, b"{}")
        self.assertEqual(len(calls), handler.RESULT_UPLOAD_MAX_ATTEMPTS)
        self.assertEqual(ctx.exception.error_code, "RESULT_UPLOAD_FAILED")

    def test_rejects_non_http_url(self):
        with self.assertRaisesRegex(handler.ResultUploadError, "non-http"):
            handler._put_result("file:///tmp/out.json", b"{}")


class DeliverResultTest(SimpleTestCase):
    """``handler._deliver_result`` envelope + slim job response."""

    INPUTS = {
        "scan_pk": 42,
        "result_url": "https://s3.example/put",
        "result_key": "processing/42/x/1/1/jobs/detect/result.json",
    }

    def test_uploads_envelope_and_returns_key_only(self):
        result = {
            "detections": [{"page_index": 0}, {"page_index": 1}],
            "page_count": 7,
            "duration_ms": 900,
        }
        uploaded = {}

        def fake_put(url, body):
            uploaded["url"] = url
            uploaded["body"] = body

        with mock.patch.object(handler, "_put_result", side_effect=fake_put):
            response = handler._deliver_result(
                result, self.INPUTS, "detect", {"id": "job-9"}
            )

        envelope = json.loads(uploaded["body"])
        self.assertEqual(uploaded["url"], self.INPUTS["result_url"])
        self.assertEqual(
            envelope["schema_version"], handler.RESULT_SCHEMA_VERSION
        )
        self.assertEqual(envelope["action"], "detect")
        self.assertEqual(envelope["scan_pk"], 42)
        self.assertEqual(envelope["job_id"], "job-9")
        self.assertEqual(envelope["payload"], result)

        # The response carries no copy of the payload: one source of
        # truth, and no ~20 MB cap to design around.
        self.assertEqual(response["status"], "succeed")
        self.assertEqual(response["action"], "detect")
        self.assertEqual(response["result_key"], self.INPUTS["result_key"])
        self.assertNotIn("detections", response)
        self.assertEqual(response["bytes"], len(uploaded["body"]))
        self.assertEqual(
            response["sha256"],
            hashlib.sha256(uploaded["body"]).hexdigest(),
        )
        self.assertEqual(response["duration_ms"], 900)
        self.assertEqual(response["page_count"], 7)

    def test_analyze_timings_ride_along(self):
        result = {"results": [{"pdf_page": 1}], "duration_ms": 5}
        with mock.patch.object(handler, "_put_result") as put:
            response = handler._deliver_result(
                result, self.INPUTS, "analyze", {"id": "job-9"}
            )
        envelope = json.loads(put.call_args.args[1])
        self.assertEqual(envelope["action"], "analyze")
        self.assertEqual(envelope["payload"], result)
        self.assertEqual(response["action"], "analyze")
        self.assertEqual(response["duration_ms"], 5)


class HandlerResultModeTest(SimpleTestCase):
    """``handler.handler`` picks inline vs. S3 delivery per job."""

    def setUp(self):
        # The module-level preload saw no CUDA (see _load_handler), and
        # handler() refuses jobs without a GPU. Flip it for these tests:
        # what's under test is result delivery, not GPU fitness.
        mock.patch.object(handler, "_CUDA_AVAILABLE", True).start()
        mock.patch.object(
            handler,
            "_ACTIONS",
            {
                "detect": lambda inputs, tmp_dir: {
                    "detections": [{"page_index": 0}],
                    "duration_ms": 12,
                }
            },
        ).start()
        self.addCleanup(mock.patch.stopall)

    def test_inline_when_no_result_url(self):
        out = handler.handler({"id": "j", "input": {"action": "detect"}})
        self.assertEqual(out["detections"], [{"page_index": 0}])
        self.assertNotIn("result_key", out)

    def test_uploads_when_result_url_present(self):
        with mock.patch.object(handler, "_put_result") as put:
            out = handler.handler(
                {
                    "id": "j",
                    "input": {
                        "action": "detect",
                        "scan_pk": 1,
                        "result_url": "https://s3.example/put",
                        "result_key": "k.json",
                    },
                }
            )
        put.assert_called_once()
        self.assertEqual(out["result_key"], "k.json")
        self.assertNotIn("detections", out)
        # Worker meta rides along either way.
        self.assertIn("gpu_available", out)

    def test_upload_failure_becomes_a_structured_error(self):
        with mock.patch.object(
            handler,
            "_put_result",
            side_effect=handler.ResultUploadError("s3 down"),
        ):
            out = handler.handler(
                {
                    "id": "j",
                    "input": {
                        "action": "detect",
                        "result_url": "https://s3.example/put",
                        "result_key": "k.json",
                    },
                }
            )
        self.assertEqual(out["error_code"], "RESULT_UPLOAD_FAILED")
        self.assertIn("s3 down", out["error"])

    def test_expired_url_reports_its_own_code(self):
        with mock.patch.object(
            handler,
            "_put_result",
            side_effect=handler.ResultUrlExpiredError("403"),
        ):
            out = handler.handler(
                {
                    "id": "j",
                    "input": {
                        "action": "detect",
                        "result_url": "https://s3.example/put",
                        "result_key": "k.json",
                    },
                }
            )
        self.assertEqual(out["error_code"], "RESULT_URL_EXPIRED")
