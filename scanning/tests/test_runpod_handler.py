"""Tests for the standalone RunPod GPU-worker handler.

``scanning/runpod/handler.py`` is a separate deploy artifact: only that
one file is copied into the worker image, and it imports the GPU stack
(``runpod``/``torch``/``blackletter``) at module scope and calls
``_preload()`` on import. None of that is installed in the scanning test
environment, so :func:`_load_handler` injects lightweight stubs into
``sys.modules`` before importing the module from its file path. The
torch stub reports no CUDA, which makes ``_preload()`` return early
before it touches any real model code.

These exercise the resumable download path in ``_download_pdf`` without
real sockets: ``requests.get`` is replaced with a fake that yields chunks
and can simulate a connection drop, a Range-ignoring server, an expired
URL, and a short body. The result-delivery path is covered the same way,
with ``requests.put`` standing in for the presigned PUT target.
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
    }
    with mock.patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location(
            "_runpod_handler_under_test", _HANDLER_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


handler = _load_handler()


class _FakeResponse:
    """Minimal stand-in for a streamed ``requests`` response.

    Yields ``chunks`` from ``iter_content`` and, if ``then_raise`` is
    set, raises it after the last chunk to simulate a dropped
    connection mid-transfer.
    """

    def __init__(
        self, status_code=200, headers=None, chunks=(), then_raise=None
    ):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = list(chunks)
        self._then_raise = then_raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(str(self.status_code))

    def iter_content(self, chunk_size=None):
        yield from self._chunks
        if self._then_raise is not None:
            raise self._then_raise


class DownloadPdfTest(SimpleTestCase):
    """Resumable-download behaviour of ``handler._download_pdf``."""

    URL = "https://example.com/presigned/scan.pdf"

    def setUp(self):
        import tempfile

        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.dest = Path(self._tmpdir.name) / "input.pdf"
        # No real sleeps / jitter during tests.
        self._sleep = mock.patch.object(handler.time, "sleep").start()
        mock.patch.object(handler.random, "uniform", return_value=0.0).start()
        self.addCleanup(mock.patch.stopall)

    def _patch_get(self, responses):
        """Patch ``requests.get`` to return ``responses`` in order and
        record the headers each call was made with."""
        calls = []
        it = iter(responses)

        def fake_get(url, headers=None, stream=None, timeout=None):
            calls.append({"url": url, "headers": headers or {}})
            return next(it)

        patcher = mock.patch.object(
            handler.requests, "get", side_effect=fake_get
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_simple_download(self):
        body = b"%PDF-1.7\n" + b"x" * 100
        self._patch_get(
            [_FakeResponse(200, {"Content-Length": str(len(body))}, [body])]
        )

        handler._download_pdf(self.URL, self.dest)

        self.assertEqual(self.dest.read_bytes(), body)

    def test_resume_after_dropped_connection(self):
        head, tail = b"%PDF-1.7\nfirst-half", b"second-half-EOF"
        full = head + tail
        calls = self._patch_get(
            [
                # Drops after sending the head.
                _FakeResponse(
                    200,
                    {"Content-Length": str(len(full))},
                    [head],
                    then_raise=requests.exceptions.ChunkedEncodingError(
                        "boom"
                    ),
                ),
                # Resume: 206 with the remaining bytes.
                _FakeResponse(
                    206,
                    {
                        "Content-Range": f"bytes {len(head)}-{len(full) - 1}/{len(full)}",
                        "Content-Length": str(len(tail)),
                    },
                    [tail],
                ),
            ]
        )

        handler._download_pdf(self.URL, self.dest)

        self.assertEqual(self.dest.read_bytes(), full)
        # Second request resumed from where the first left off.
        self.assertNotIn("Range", calls[0]["headers"])
        self.assertEqual(
            calls[1]["headers"].get("Range"), f"bytes={len(head)}-"
        )
        self._sleep.assert_called_once()

    def test_server_ignores_range_restarts_clean(self):
        head = b"%PDF-1.7\nhead"
        full = head + b"-rest-of-file-EOF"
        self._patch_get(
            [
                _FakeResponse(
                    200,
                    {"Content-Length": str(len(full))},
                    [head],
                    then_raise=requests.exceptions.ConnectionError("reset"),
                ),
                # Server ignored Range and re-sent the WHOLE body as 200.
                _FakeResponse(200, {"Content-Length": str(len(full))}, [full]),
            ]
        )

        handler._download_pdf(self.URL, self.dest)

        # File must be the body exactly once, not head + full.
        self.assertEqual(self.dest.read_bytes(), full)

    def test_416_treated_as_complete(self):
        full = b"%PDF-1.7\ncomplete-body"
        self._patch_get(
            [
                # Drops right at the end, after all bytes are on disk.
                _FakeResponse(
                    200,
                    {"Content-Length": str(len(full))},
                    [full],
                    then_raise=requests.exceptions.ChunkedEncodingError(
                        "late"
                    ),
                ),
                # Resume past EOF -> 416.
                _FakeResponse(416, {}, []),
            ]
        )

        handler._download_pdf(self.URL, self.dest)

        self.assertEqual(self.dest.read_bytes(), full)

    def test_403_fails_fast_without_retry(self):
        calls = self._patch_get([_FakeResponse(403, {}, [])])

        with self.assertRaisesRegex(RuntimeError, "403"):
            handler._download_pdf(self.URL, self.dest)

        # No retry, no backoff sleep on a dead URL.
        self.assertEqual(len(calls), 1)
        self._sleep.assert_not_called()

    def test_incomplete_download_raises(self):
        # 200 advertises 100 bytes but the stream ends cleanly at 50.
        self._patch_get(
            [_FakeResponse(200, {"Content-Length": "100"}, [b"x" * 50])]
        )

        with self.assertRaisesRegex(
            RuntimeError, r"incomplete download: 50/100"
        ):
            handler._download_pdf(self.URL, self.dest)

    def test_gives_up_after_max_attempts(self):
        drop = requests.exceptions.ChunkedEncodingError("always drops")
        responses = [
            _FakeResponse(
                200, {"Content-Length": "10"}, [b"x"], then_raise=drop
            )
            for _ in range(handler.DOWNLOAD_MAX_ATTEMPTS)
        ]
        calls = self._patch_get(responses)

        with self.assertRaises(requests.exceptions.ChunkedEncodingError):
            handler._download_pdf(self.URL, self.dest)

        self.assertEqual(len(calls), handler.DOWNLOAD_MAX_ATTEMPTS)

    def test_rejects_non_http_url(self):
        with self.assertRaisesRegex(ValueError, "non-http"):
            handler._download_pdf("file:///etc/passwd", self.dest)


class ExpectedTotalTest(SimpleTestCase):
    """``handler._expected_total`` header parsing."""

    def _resp(self, headers):
        return _FakeResponse(headers=headers)

    def test_content_range_total_wins_on_206(self):
        resp = self._resp(
            {"Content-Range": "bytes 100-199/2048", "Content-Length": "100"}
        )
        self.assertEqual(handler._expected_total(resp), 2048)

    def test_content_length_used_on_200(self):
        self.assertEqual(
            handler._expected_total(self._resp({"Content-Length": "512"})), 512
        )

    def test_returns_none_when_unparseable(self):
        self.assertIsNone(handler._expected_total(self._resp({})))
        self.assertIsNone(
            handler._expected_total(self._resp({"Content-Length": "??"}))
        )
        self.assertIsNone(
            handler._expected_total(self._resp({"Content-Range": "bytes */*"}))
        )


class ValidatePdfTest(SimpleTestCase):
    """``handler._validate_pdf`` structural validation."""

    def setUp(self):
        import tempfile

        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.path = Path(self._tmpdir.name) / "input.pdf"

    @staticmethod
    def _pdf_bytes(pages=1):
        """Build a real, complete PDF of ``pages`` pages."""
        import fitz

        doc = fitz.open()
        for _ in range(pages):
            doc.new_page()
        data = doc.tobytes()
        doc.close()
        return data

    def test_valid_single_page(self):
        self.path.write_bytes(self._pdf_bytes(1))
        self.assertEqual(handler._validate_pdf(self.path), 1)

    def test_valid_multi_page(self):
        self.path.write_bytes(self._pdf_bytes(5))
        self.assertEqual(handler._validate_pdf(self.path), 5)

    def test_truncated_pdf_missing_eof(self):
        full = self._pdf_bytes(3)
        # Lop off the tail so the %%EOF trailer is gone, simulating a
        # download that died mid-transfer.
        self.path.write_bytes(full[: len(full) // 2])

        with self.assertRaisesRegex(ValueError, "truncated"):
            handler._validate_pdf(self.path)

    def test_empty_file(self):
        self.path.write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "empty"):
            handler._validate_pdf(self.path)

    def test_not_a_pdf(self):
        self.path.write_bytes(b"<html>nope</html>\n%%EOF")
        with self.assertRaisesRegex(ValueError, "not a PDF"):
            handler._validate_pdf(self.path)


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
