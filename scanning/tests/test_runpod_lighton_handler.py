"""Tests for the standalone LightOn RunPod GPU-worker handler.

``scanning/runpod-lighton/handler.py`` is a separate deploy artifact:
only that one file is copied into the worker image, it imports ``runpod``
at module scope, and it calls ``_preload()`` on import. Neither is
available in the scanning test environment, so :func:`_load_handler`
injects a stub and forces ``shutil.which`` to find no ``nvidia-smi``,
which makes ``_preload()`` return early before it tries to start a vLLM
server.

What's covered here is everything that runs without a GPU: input
validation, the coordinate-space reproduction (render, redact, crop),
the token-budget policy, and the handler's dispatch/error contract.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path
from unittest import mock

import fitz
import requests
from django.test import SimpleTestCase
from PIL import Image

_HANDLER_PATH = (
    Path(__file__).resolve().parent.parent / "runpod-lighton" / "handler.py"
)


def _load_handler():
    """Import the handler module with its worker-only deps stubbed."""
    stubs = {"runpod": mock.MagicMock()}
    with (
        mock.patch.dict(sys.modules, stubs),
        mock.patch("shutil.which", return_value=None),
    ):
        spec = importlib.util.spec_from_file_location(
            "_lighton_handler_under_test", _HANDLER_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


handler = _load_handler()


def _one_page_pdf(width_pt: float = 612, height_pt: float = 792) -> bytes:
    """A single-page PDF with a black bar near the top."""
    doc = fitz.open()
    page = doc.new_page(width=width_pt, height=height_pt)
    page.draw_rect(
        fitz.Rect(50, 50, width_pt - 50, 120), color=(0, 0, 0), fill=(0, 0, 0)
    )
    data = doc.tobytes()
    doc.close()
    return data


class TestValidateCrops(SimpleTestCase):
    def test_derives_area_from_bbox(self):
        out = handler._validate_crops(
            [{"key": "a", "page_index": 0, "bbox": [0, 0, 10, 20]}]
        )
        self.assertEqual(out[0]["area"], 200)

    def test_keeps_caller_supplied_area(self):
        out = handler._validate_crops(
            [
                {
                    "key": "a",
                    "page_index": 0,
                    "bbox": [0, 0, 10, 20],
                    "area": 999,
                }
            ]
        )
        self.assertEqual(out[0]["area"], 999)

    def test_coerces_numeric_strings(self):
        out = handler._validate_crops(
            [{"key": "a", "page_index": "3", "bbox": ["1", "2", "3", "4"]}]
        )
        self.assertEqual(out[0]["page_index"], 3)
        self.assertEqual(out[0]["bbox"], [1, 2, 3, 4])

    def test_rejects_empty_list(self):
        with self.assertRaises(ValueError):
            handler._validate_crops([])

    def test_rejects_duplicate_keys(self):
        with self.assertRaisesMessage(ValueError, "duplicate crop key"):
            handler._validate_crops(
                [
                    {"key": "a", "page_index": 0, "bbox": [0, 0, 1, 1]},
                    {"key": "a", "page_index": 1, "bbox": [0, 0, 1, 1]},
                ]
            )

    def test_rejects_missing_key(self):
        with self.assertRaises(ValueError):
            handler._validate_crops([{"page_index": 0, "bbox": [0, 0, 1, 1]}])

    def test_rejects_negative_page_index(self):
        with self.assertRaises(ValueError):
            handler._validate_crops(
                [{"key": "a", "page_index": -1, "bbox": [0, 0, 1, 1]}]
            )

    def test_rejects_malformed_bbox(self):
        with self.assertRaises(ValueError):
            handler._validate_crops(
                [{"key": "a", "page_index": 0, "bbox": [0, 0, 1]}]
            )

    def test_rejects_more_than_max_crops(self):
        many = [
            {"key": f"k{i}", "page_index": 0, "bbox": [0, 0, 1, 1]}
            for i in range(3)
        ]
        with mock.patch.object(handler, "MAX_CROPS", 2):
            with self.assertRaisesMessage(ValueError, "HANDLER_MAX_CROPS"):
                handler._validate_crops(many)


class TestValidateRedactions(SimpleTestCase):
    def test_coerces_json_string_keys_to_int(self):
        out = handler._validate_redactions({"6": [[1, 2, 3, 4]]})
        self.assertEqual(out, {6: [[1, 2, 3, 4]]})

    def test_empty_is_empty(self):
        self.assertEqual(handler._validate_redactions(None), {})
        self.assertEqual(handler._validate_redactions({}), {})

    def test_rejects_malformed_bbox(self):
        with self.assertRaises(ValueError):
            handler._validate_redactions({"0": [[1, 2, 3]]})

    def test_rejects_non_mapping(self):
        with self.assertRaises(ValueError):
            handler._validate_redactions([[1, 2, 3, 4]])

    def test_rejects_a_non_numeric_bbox_as_bad_input(self):
        # ValueError specifically, like _validate_crops: the handler
        # maps that to a terminal BAD_INPUT, while the TypeError that
        # int(None) would raise later falls through to a bare crash.
        for rect in ([1, 2, None, 4], [1, 2, "x", 4]):
            with self.subTest(rect=rect):
                with self.assertRaisesMessage(ValueError, "non-numeric"):
                    handler._validate_redactions({"0": [rect]})

    def test_coerces_numeric_strings(self):
        # Rejected before the download rather than mid-render, so the
        # values reaching _apply_redactions are already numbers.
        out = handler._validate_redactions({"0": [["1", "2", "3", "4"]]})
        self.assertEqual(out, {0: [[1, 2, 3, 4]]})


class TestTokenBudget(SimpleTestCase):
    def test_floor(self):
        self.assertEqual(handler._token_budget(0), 128)

    def test_ceiling(self):
        self.assertEqual(handler._token_budget(10_000_000), 1024)

    def test_scales_with_area(self):
        self.assertEqual(handler._token_budget(250_000), 500)


class TestRenderPage(SimpleTestCase):
    """The renderer must reproduce the pipeline's canonical space."""

    def test_letter_page_renders_at_canonical_size(self):
        doc = fitz.open(stream=_one_page_pdf(), filetype="pdf")
        try:
            img = handler._render_page(doc, 0, 1700, 2200)
        finally:
            doc.close()
        self.assertEqual(img.size, (1700, 2200))
        self.assertEqual(img.mode, "RGB")

    def test_odd_aspect_ratio_is_forced_not_letterboxed(self):
        # A square page must still come out 1700x2200. Letterboxing
        # here would shift every bbox the caller sends.
        doc = fitz.open(stream=_one_page_pdf(600, 600), filetype="pdf")
        try:
            img = handler._render_page(doc, 0, 1700, 2200)
        finally:
            doc.close()
        self.assertEqual(img.size, (1700, 2200))


class TestApplyRedactions(SimpleTestCase):
    def test_paints_black(self):
        img = Image.new("RGB", (100, 100), (255, 255, 255))
        handler._apply_redactions(img, [[10, 10, 50, 50]])
        self.assertEqual(img.getpixel((30, 30)), (0, 0, 0))
        self.assertEqual(img.getpixel((80, 80)), (255, 255, 255))

    def test_empty_is_a_noop(self):
        img = Image.new("RGB", (10, 10), (255, 255, 255))
        handler._apply_redactions(img, [])
        self.assertEqual(img.getpixel((5, 5)), (255, 255, 255))


class TestCropPng(SimpleTestCase):
    def _size(self, png: bytes) -> tuple[int, int]:
        import io

        with Image.open(io.BytesIO(png)) as im:
            return im.size

    def test_normal_crop_keeps_its_size(self):
        img = Image.new("RGB", (1700, 2200), (255, 255, 255))
        png = handler._crop_png(img, [100, 100, 400, 300])
        self.assertEqual(self._size(png), (300, 200))

    def test_tiny_crop_is_scaled_up(self):
        # A single-glyph region would otherwise crash the vision tower.
        img = Image.new("RGB", (1700, 2200), (255, 255, 255))
        png = handler._crop_png(img, [10, 10, 20, 30])
        w, h = self._size(png)
        self.assertGreaterEqual(min(w, h), handler.MIN_CROP_SIDE)
        # aspect preserved: 10x20 -> 64x128
        self.assertAlmostEqual(w / h, 10 / 20, places=2)

    def test_bbox_is_clamped_to_image_bounds(self):
        img = Image.new("RGB", (100, 100), (255, 255, 255))
        png = handler._crop_png(img, [-50, -50, 5000, 5000])
        self.assertEqual(self._size(png), (100, 100))

    def test_inverted_bbox_does_not_crash(self):
        img = Image.new("RGB", (100, 100), (255, 255, 255))
        png = handler._crop_png(img, [80, 80, 10, 10])
        self.assertGreaterEqual(min(self._size(png)), 1)


class TestRedactUrls(SimpleTestCase):
    def test_masks_pdf_url_and_summarises_crops(self):
        out = handler._redact_urls(
            {
                "pdf_url": "https://s3/x?X-Amz-Signature=secret",
                "crops": [{"key": "a"}, {"key": "b"}],
                "action": "read_crops",
            }
        )
        self.assertEqual(out["pdf_url"], "***")
        self.assertNotIn("secret", str(out))
        self.assertEqual(out["crops"], "<2 entries>")
        self.assertEqual(out["action"], "read_crops")


class TestHandlerDispatch(SimpleTestCase):
    """The error contract the caller classifies on."""

    def test_missing_action_is_bad_input(self):
        out = handler.handler({"input": {}})
        self.assertEqual(out["error_code"], "BAD_INPUT")

    def test_no_gpu_is_reported_before_dispatch(self):
        with mock.patch.object(handler, "_GPU_AVAILABLE", False):
            out = handler.handler({"input": {"action": "read_crops"}})
        self.assertEqual(out["error_code"], "NO_GPU")

    def test_unknown_action(self):
        with (
            mock.patch.object(handler, "_GPU_AVAILABLE", True),
            mock.patch.object(handler, "_VLLM_READY", True),
            mock.patch.object(handler, "_vllm_healthy", return_value=True),
        ):
            out = handler.handler({"input": {"action": "nope"}})
        self.assertEqual(out["error_code"], "UNKNOWN_ACTION")

    def test_unhealthy_vllm_is_transient(self):
        with (
            mock.patch.object(handler, "_GPU_AVAILABLE", True),
            mock.patch.object(handler, "_VLLM_READY", True),
            mock.patch.object(handler, "_vllm_healthy", return_value=False),
        ):
            out = handler.handler({"input": {"action": "read_crops"}})
        self.assertEqual(out["error_code"], "VLLM_UNHEALTHY")

    def test_bad_action_input_is_terminal_not_a_crash(self):
        # A missing pdf_url must come back as BAD_INPUT rather than
        # raising, so the caller fails the job instead of re-queueing
        # it forever.
        with (
            mock.patch.object(handler, "_GPU_AVAILABLE", True),
            mock.patch.object(handler, "_VLLM_READY", True),
            mock.patch.object(handler, "_vllm_healthy", return_value=True),
        ):
            out = handler.handler(
                {"input": {"action": "read_crops", "crops": []}}
            )
        self.assertEqual(out["error_code"], "BAD_INPUT")

    def test_every_response_carries_worker_meta(self):
        out = handler.handler({"input": {}})
        self.assertIn("worker_boot_ms", out)
        self.assertIn("worker_uptime_ms", out)
        self.assertIn("gpu_available", out)


class TestDownloadPdf(SimpleTestCase):
    """The resumable download's status handling.

    Only the branches this worker owns; the retry/backoff loop itself
    is shared with ``scanning/runpod/handler.py``.
    """

    def _response(self, status, body=b"", headers=None):
        """Build a stand-in for a streaming ``requests`` response.

        :param status: HTTP status code.
        :param body: Bytes the response streams.
        :param headers: Response headers.
        :returns: A context-manager mock.
        """
        resp = mock.MagicMock()
        resp.status_code = status
        resp.headers = headers or {}
        resp.iter_content.return_value = [body] if body else []
        resp.raise_for_status.side_effect = (
            None
            if status < 400
            else requests.exceptions.HTTPError(f"{status}", response=resp)
        )
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    def _download(self, responses, dest):
        """Run ``_download_pdf`` against a scripted response sequence.

        :param responses: Responses returned in order by requests.get.
        :param dest: Path to download to.
        :returns: The patched ``requests.get`` mock.
        """
        with (
            mock.patch.object(
                handler.requests, "get", side_effect=responses
            ) as mock_get,
            mock.patch.object(handler.time, "sleep"),
        ):
            handler._download_pdf("https://x/y.pdf", dest)
        return mock_get

    def test_a_rangeless_416_is_an_error_not_a_finished_download(self):
        # 416 only means "already complete" as an answer to a Range we
        # sent. On the first attempt none was sent, so treating it as
        # success returns with no file on disk and the real cause shows
        # up later as a confusing fitz error.
        dest = Path(tempfile.mkdtemp()) / "input.pdf"
        with self.assertRaises(Exception) as ctx:
            self._download([self._response(416)] * 5, dest)

        self.assertNotIsInstance(ctx.exception, AssertionError)
        self.assertFalse(dest.exists())

    def _dropped(self, written, total):
        """A response that streams some bytes and then loses the connection.

        :param written: Bytes delivered before the drop.
        :param total: Value for the Content-Length header.
        :returns: A response mock.
        """

        def chunks():
            yield written
            raise requests.exceptions.ConnectionError("reset")

        resp = self._response(200, headers={"Content-Length": str(total)})
        resp.iter_content.return_value = chunks()
        return resp

    def test_a_ranged_416_means_the_file_is_already_there(self):
        dest = Path(tempfile.mkdtemp()) / "input.pdf"

        # The first attempt writes some bytes and then drops, so the
        # retry resumes with a Range header. A 416 answering *that* is
        # the genuine "already complete" case.
        self._download(
            [self._dropped(b"partial data", 999), self._response(416)], dest
        )

        self.assertEqual(dest.read_bytes(), b"partial data")

    def test_an_ignored_range_restarts_from_byte_zero(self):
        dest = Path(tempfile.mkdtemp()) / "input.pdf"
        dropped = self._dropped(b"stale partial", 99)
        # The resume gets a 200 rather than a 206: the server ignored
        # the Range and is resending the whole body.
        resend = self._response(
            200, body=b"whole file", headers={"Content-Length": "10"}
        )
        self._download([dropped, resend], dest)

        # The stale bytes are gone, not appended to.
        self.assertEqual(dest.read_bytes(), b"whole file")
