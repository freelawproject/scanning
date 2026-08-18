"""Tests for the transfer/validation code shared by the worker images.

``scanning/runpod_common.py`` is copied into every RunPod worker image
next to its ``handler.py`` (which imports it as a top-level module).
It has no Django or GPU-stack dependencies, so unlike the handlers it
can be imported directly.

These exercise the resumable download path in ``download_pdf`` without
real sockets: ``requests.get`` is replaced with a fake that yields
chunks and can simulate a connection drop, a Range-ignoring server, an
expired URL, and a short body. Moved here from
``test_runpod_handler.py`` when the previously per-handler copies were
extracted into the shared module.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import requests
from django.test import SimpleTestCase

from scanning import runpod_common


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
    """Resumable-download behaviour of ``runpod_common.download_pdf``."""

    URL = "https://example.com/presigned/scan.pdf"

    def setUp(self):
        import tempfile

        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.dest = Path(self._tmpdir.name) / "input.pdf"
        # No real sleeps / jitter during tests.
        self._sleep = mock.patch.object(runpod_common.time, "sleep").start()
        mock.patch.object(
            runpod_common.random, "uniform", return_value=0.0
        ).start()
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
            runpod_common.requests, "get", side_effect=fake_get
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_simple_download(self):
        body = b"%PDF-1.7\n" + b"x" * 100
        self._patch_get(
            [_FakeResponse(200, {"Content-Length": str(len(body))}, [body])]
        )

        runpod_common.download_pdf(self.URL, self.dest)

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

        runpod_common.download_pdf(self.URL, self.dest)

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

        runpod_common.download_pdf(self.URL, self.dest)

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

        runpod_common.download_pdf(self.URL, self.dest)

        self.assertEqual(self.dest.read_bytes(), full)

    def test_403_fails_fast_without_retry(self):
        calls = self._patch_get([_FakeResponse(403, {}, [])])

        with self.assertRaisesRegex(RuntimeError, "403"):
            runpod_common.download_pdf(self.URL, self.dest)

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
            runpod_common.download_pdf(self.URL, self.dest)

    def test_gives_up_after_max_attempts(self):
        drop = requests.exceptions.ChunkedEncodingError("always drops")
        responses = [
            _FakeResponse(
                200, {"Content-Length": "10"}, [b"x"], then_raise=drop
            )
            for _ in range(runpod_common.DOWNLOAD_MAX_ATTEMPTS)
        ]
        calls = self._patch_get(responses)

        with self.assertRaises(requests.exceptions.ChunkedEncodingError):
            runpod_common.download_pdf(self.URL, self.dest)

        self.assertEqual(len(calls), runpod_common.DOWNLOAD_MAX_ATTEMPTS)

    def test_rejects_non_http_url(self):
        with self.assertRaisesRegex(ValueError, "non-http"):
            runpod_common.download_pdf("file:///etc/passwd", self.dest)


class ExpectedTotalTest(SimpleTestCase):
    """``runpod_common.expected_total`` header parsing."""

    def _resp(self, headers):
        return _FakeResponse(headers=headers)

    def test_content_range_total_wins_on_206(self):
        resp = self._resp(
            {"Content-Range": "bytes 100-199/2048", "Content-Length": "100"}
        )
        self.assertEqual(runpod_common.expected_total(resp), 2048)

    def test_content_length_used_on_200(self):
        self.assertEqual(
            runpod_common.expected_total(
                self._resp({"Content-Length": "512"})
            ),
            512,
        )

    def test_returns_none_when_unparseable(self):
        self.assertIsNone(runpod_common.expected_total(self._resp({})))
        self.assertIsNone(
            runpod_common.expected_total(self._resp({"Content-Length": "??"}))
        )
        self.assertIsNone(
            runpod_common.expected_total(
                self._resp({"Content-Range": "bytes */*"})
            )
        )


class ValidatePdfTest(SimpleTestCase):
    """``runpod_common.validate_pdf`` structural validation."""

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
        self.assertEqual(runpod_common.validate_pdf(self.path), 1)

    def test_valid_multi_page(self):
        self.path.write_bytes(self._pdf_bytes(5))
        self.assertEqual(runpod_common.validate_pdf(self.path), 5)

    def test_truncated_pdf_missing_eof(self):
        full = self._pdf_bytes(3)
        # Lop off the tail so the %%EOF trailer is gone, simulating a
        # download that died mid-transfer.
        self.path.write_bytes(full[: len(full) // 2])

        with self.assertRaisesRegex(ValueError, "truncated"):
            runpod_common.validate_pdf(self.path)

    def test_empty_file(self):
        self.path.write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "empty"):
            runpod_common.validate_pdf(self.path)

    def test_not_a_pdf(self):
        self.path.write_bytes(b"<html>nope</html>\n%%EOF")
        with self.assertRaisesRegex(ValueError, "not a PDF"):
            runpod_common.validate_pdf(self.path)
