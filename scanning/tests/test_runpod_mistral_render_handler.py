"""Tests for the standalone RunPod Mistral render worker handler.

``scanning/runpod-mistral-render/handler.py`` is a separate deploy
artifact: only that one file is copied into the worker image, beside
``runpod_common.py`` and ``perpendiculars.py``, and it imports the
worker stack (``runpod``) that isn't installed in the scanning test
environment. :func:`_load_handler` injects a stub for it into
``sys.modules`` before importing the module from its file path, and
aliases the real shared modules under the top-level names the image
gives them.

No HTTP: the shard download and the two Mistral calls (the page
upload and the delete) are patched on the handler module. The render
is real, over a synthetic PDF, so a test can check that a worker page
is the daemon's page: 1700x2200 RGB. The pool is run in process (one
process) except in the one test of the pool itself, which forks.
"""

from __future__ import annotations

import importlib.util
import io
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

import fitz
from django.test import SimpleTestCase
from PIL import Image

from scanning import perpendiculars, runpod_common

_HANDLER_PATH = (
    Path(__file__).resolve().parent.parent
    / "runpod-mistral-render"
    / "handler.py"
)
_MODULE_NAME = "_runpod_mistral_render_handler_under_test"


def _load_handler():
    """Import the handler module with its worker-only dep stubbed.

    The module is registered in ``sys.modules`` under its test name
    because the pool test pickles ``render_chunk`` by reference, and a
    forked child resolves it through that name.
    """
    stubs = {
        "runpod": mock.MagicMock(),
        # The real shared modules, under the top-level names the
        # worker image gives them.
        "runpod_common": runpod_common,
        "perpendiculars": perpendiculars,
    }
    with mock.patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location(
            _MODULE_NAME, _HANDLER_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    # Registered after the patch, which restores ``sys.modules`` on
    # exit and would take the name out again.
    sys.modules[_MODULE_NAME] = module
    return module


handler = _load_handler()


def write_pdf(path: Path, pages: int = 2, size=(612, 792)) -> None:
    """Write a PDF of white pages with a black box on each."""
    with fitz.open() as doc:
        for index in range(pages):
            page = doc.new_page(width=size[0], height=size[1])
            page.insert_text((72, 72), f"Page {index + 1}", fontsize=12)
            page.draw_rect(
                fitz.Rect(
                    size[0] * 0.1, size[1] * 0.1, size[0] * 0.4, size[1] * 0.4
                ),
                fill=(0, 0, 0),
                width=0,
            )
        doc.save(str(path))


class _Uploads:
    """A fake of :func:`handler.upload_png` that records every call."""

    def __init__(self, script=None):
        self.calls: list[tuple[str, bytes]] = []
        self.script = list(script or [])

    def __call__(self, api_key, name, png):
        self.calls.append((name, png))
        if self.script:
            answer = self.script.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return answer
        return f"id-{name}"


class _RenderCase(SimpleTestCase):
    """Shared plumbing: a shard on disk, a key, no HTTP, one process."""

    PAGES = 3

    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="mistral-render-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.pdf = self.tmp / "shard.pdf"
        write_pdf(self.pdf, pages=self.PAGES)
        self.enterContext(
            mock.patch.dict(os.environ, {"MISTRAL_API_KEY": "key-1"})
        )
        self.enterContext(
            mock.patch.object(
                handler,
                "download_pdf",
                side_effect=lambda url, dest: shutil.copy(self.pdf, dest),
            )
        )
        self.uploads = _Uploads()
        self.enterContext(
            mock.patch.object(handler, "upload_png", self.uploads)
        )
        self.deletes = self.enterContext(
            mock.patch.object(handler, "delete_file", return_value=True)
        )
        self.enterContext(mock.patch.object(handler, "RENDER_PROCESSES", 1))
        self.enterContext(
            mock.patch.object(handler, "UPLOAD_BACKOFF_SECONDS", 0)
        )

    def run_job(self, **inputs):
        payload = {
            "action": "render",
            "pdf_url": "https://signed/shard.pdf",
            "file_prefix": "scan1-job2-a1-",
            **inputs,
        }
        return handler.handler({"id": "rp-1", "input": payload})


class TestDispatch(_RenderCase):
    """The action surface and the input checks."""

    def test_a_missing_action_is_bad_input(self):
        out = handler.handler({"id": "rp-1", "input": {}})
        self.assertEqual(out["error_code"], "BAD_INPUT")
        self.assertFalse(out["gpu_available"])

    def test_an_unknown_action_is_refused(self):
        out = handler.handler({"id": "rp-1", "input": {"action": "ocr"}})
        self.assertEqual(out["error_code"], "UNKNOWN_ACTION")

    def test_a_missing_pdf_url_is_bad_input(self):
        out = self.run_job(pdf_url=None)
        self.assertEqual(out["error_code"], "BAD_INPUT")
        self.assertIn("pdf_url", out["error"])
        self.assertEqual(self.uploads.calls, [])

    def test_a_missing_file_prefix_is_bad_input(self):
        out = self.run_job(file_prefix="")
        self.assertEqual(out["error_code"], "BAD_INPUT")
        self.assertIn("file_prefix", out["error"])

    def test_a_png_level_outside_the_range_is_bad_input(self):
        out = self.run_job(png_level=12)
        self.assertEqual(out["error_code"], "BAD_INPUT")
        out = self.run_job(png_level="fast")
        self.assertEqual(out["error_code"], "BAD_INPUT")

    def test_another_page_count_than_the_pdf_has_is_bad_input(self):
        out = self.run_job(page_count=self.PAGES + 1)
        self.assertEqual(out["error_code"], "BAD_INPUT")
        self.assertIn("expected", out["error"])
        self.assertEqual(self.uploads.calls, [])

    def test_a_missing_key_is_not_configured_and_uploads_nothing(self):
        with mock.patch.dict(os.environ, {"MISTRAL_API_KEY": ""}):
            out = self.run_job()
        self.assertEqual(out["error_code"], handler.NOT_CONFIGURED)
        self.assertEqual(out["uploaded"], 0)
        self.assertEqual(self.uploads.calls, [])
        self.deletes.assert_not_called()


class TestRender(_RenderCase):
    """The pages go up in order, as the daemon's pages, and the answer
    names them."""

    def test_every_page_is_uploaded_in_order_as_the_canonical_png(self):
        out = self.run_job(page_count=self.PAGES)

        self.assertNotIn("error", out)
        self.assertEqual(
            out["file_ids"],
            [f"id-scan1-job2-a1-p{n}.png" for n in range(self.PAGES)],
        )
        self.assertEqual(out["page_count"], self.PAGES)
        self.assertEqual(out["png_level"], handler.DEFAULT_PNG_LEVEL)
        self.assertFalse(out["whiteout_perpendiculars"])
        self.assertEqual(out["processes"], 1)
        self.assertEqual(
            out["bytes"], sum(len(png) for _, png in self.uploads.calls)
        )
        self.assertFalse(out["gpu_available"])
        self.deletes.assert_not_called()
        names = [name for name, _ in self.uploads.calls]
        self.assertEqual(
            sorted(names), [f"scan1-job2-a1-p{n}.png" for n in range(3)]
        )
        img = Image.open(io.BytesIO(self.uploads.calls[0][1]))
        self.assertEqual(img.format, "PNG")
        self.assertEqual(img.mode, "RGB")
        self.assertEqual(img.size, (1700, 2200))
        # The box the fixture draws, and white paper beside it: no
        # grey conversion and no threshold happened.
        self.assertEqual(img.getpixel((400, 400)), (0, 0, 0))
        self.assertEqual(img.getpixel((1600, 2100)), (255, 255, 255))

    def test_the_whiteout_and_the_level_reach_the_render(self):
        with mock.patch.object(
            handler, "render_page", wraps=handler.render_page
        ) as render:
            out = self.run_job(whiteout_perpendiculars=True, png_level=6)
        self.assertTrue(out["whiteout_perpendiculars"])
        self.assertEqual(out["png_level"], 6)
        for call in render.call_args_list:
            self.assertEqual(call.kwargs, {"whiteout": True, "level": 6})

    def test_a_page_of_another_size_is_resized_to_the_canonical_size(self):
        write_pdf(self.pdf, pages=1, size=(400, 700))
        out = self.run_job()
        self.assertEqual(out["page_count"], 1)
        img = Image.open(io.BytesIO(self.uploads.calls[0][1]))
        self.assertEqual(img.size, (1700, 2200))

    def test_a_failed_page_deletes_what_went_up_and_answers_its_code(self):
        self.uploads.script = [
            "id-0",
            handler.MistralUploadError("p1: HTTP 400", "MISTRAL_HTTP_400"),
        ]
        out = self.run_job()

        self.assertEqual(out["error_code"], "MISTRAL_HTTP_400")
        self.assertEqual(out["page_count"], self.PAGES)
        self.assertEqual(out["uploaded"], 1)
        self.assertEqual(out["deleted"], 1)
        self.assertEqual(out["files_left"], [])
        self.deletes.assert_called_once_with("key-1", "id-0")
        # The shard stopped at the failed page: nothing after it went up.
        self.assertEqual(len(self.uploads.calls), 2)

    def test_a_file_a_delete_cannot_reach_is_named(self):
        self.uploads.script = [
            "id-0",
            handler.MistralUploadError("p1: HTTP 400", "MISTRAL_HTTP_400"),
        ]
        self.deletes.return_value = False
        out = self.run_job()
        self.assertEqual(out["deleted"], 0)
        self.assertEqual(out["files_left"], ["id-0"])

    def test_a_render_fault_is_answered_as_a_code_not_raised(self):
        with mock.patch.object(
            handler, "render_page", side_effect=RuntimeError("boom")
        ):
            out = self.run_job()
        self.assertEqual(out["error_code"], handler.RENDER_FAILED)
        self.assertIn("boom", out["error"])
        self.assertEqual(out["uploaded"], 0)

    def test_the_pool_renders_the_pages_over_processes(self):
        """The forked pool: the chunks go to two processes, each uploads
        its own pages, and the answer is still in page order."""
        with (
            mock.patch.object(handler, "RENDER_PROCESSES", 2),
            mock.patch.object(handler, "CHUNK_PAGES", 1),
        ):
            out = self.run_job(page_count=self.PAGES)
        self.assertNotIn("error", out)
        self.assertEqual(out["processes"], 2)
        self.assertEqual(
            out["file_ids"],
            [f"id-scan1-job2-a1-p{n}.png" for n in range(self.PAGES)],
        )
        # The uploads ran in the children, so the parent's fake saw
        # none of them; the ids came back through the pool.
        self.assertEqual(self.uploads.calls, [])


class TestPool(SimpleTestCase):
    """The pool arithmetic."""

    def test_chunks_cover_the_pages_once(self):
        with mock.patch.object(handler, "CHUNK_PAGES", 4):
            self.assertEqual(
                handler._chunks(10), [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9]]
            )
            self.assertEqual(handler._chunks(0), [])

    def test_pool_size_follows_the_cpus_the_cap_and_the_chunks(self):
        with (
            mock.patch.object(handler, "RENDER_PROCESSES", 0),
            mock.patch.object(handler, "PROCESSES_PER_CPU", 2.0),
            mock.patch.object(handler, "MAX_PROCESSES", 32),
            mock.patch.object(handler, "CHUNK_PAGES", 4),
            mock.patch.object(handler, "_cpus", return_value=4),
        ):
            self.assertEqual(handler.pool_size(100), 8)
            # Fewer chunks than processes: no idle process.
            self.assertEqual(handler.pool_size(5), 2)
        with (
            mock.patch.object(handler, "RENDER_PROCESSES", 3),
            mock.patch.object(handler, "CHUNK_PAGES", 4),
        ):
            self.assertEqual(handler.pool_size(100), 3)
        with (
            mock.patch.object(handler, "RENDER_PROCESSES", 0),
            mock.patch.object(handler, "MAX_PROCESSES", 2),
            mock.patch.object(handler, "CHUNK_PAGES", 4),
            mock.patch.object(handler, "_cpus", return_value=16),
        ):
            self.assertEqual(handler.pool_size(100), 2)


def _response(status: int, body: dict | None = None, text: str = ""):
    """A fake ``requests.Response``."""
    response = mock.MagicMock()
    response.status_code = status
    response.text = text
    response.json.return_value = body if body is not None else {}
    return response


class TestUpload(SimpleTestCase):
    """The page upload: the tries and their codes."""

    def setUp(self):
        super().setUp()
        self.enterContext(
            mock.patch.object(handler, "UPLOAD_BACKOFF_SECONDS", 0)
        )
        self.enterContext(mock.patch.object(handler, "UPLOAD_RETRIES", 2))
        self.post = self.enterContext(
            mock.patch.object(handler.requests, "post")
        )

    def test_a_landed_upload_answers_its_id(self):
        self.post.return_value = _response(200, {"id": "file-1"})
        self.assertEqual(
            handler.upload_png("key", "pre-p0.png", b"png"), "file-1"
        )
        call = self.post.call_args
        self.assertTrue(call.args[0].endswith("/v1/files"))
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer key")
        self.assertEqual(call.kwargs["data"], {"purpose": "ocr"})
        self.assertEqual(
            call.kwargs["files"]["file"], ("pre-p0.png", b"png", "image/png")
        )

    def test_a_throttled_upload_is_tried_again_then_rate_limited(self):
        self.post.return_value = _response(429, text="slow down")
        with self.assertRaises(handler.MistralUploadError) as caught:
            handler.upload_png("key", "pre-p0.png", b"png")
        self.assertEqual(caught.exception.error_code, handler.RATE_LIMITED)
        self.assertEqual(self.post.call_count, 1 + handler.UPLOAD_RETRIES)

    def test_a_5xx_is_tried_again_and_a_later_try_lands(self):
        self.post.side_effect = [
            _response(502, text="bad gateway"),
            _response(200, {"id": "file-2"}),
        ]
        self.assertEqual(
            handler.upload_png("key", "pre-p0.png", b"png"), "file-2"
        )

    def test_a_transport_failure_is_tried_again_then_unreachable(self):
        self.post.side_effect = handler.requests.ConnectionError("down")
        with self.assertRaises(handler.MistralUploadError) as caught:
            handler.upload_png("key", "pre-p0.png", b"png")
        self.assertEqual(
            caught.exception.error_code, handler.MISTRAL_UNREACHABLE
        )

    def test_another_4xx_is_refused_at_once(self):
        self.post.return_value = _response(401, text="bad key")
        with self.assertRaises(handler.MistralUploadError) as caught:
            handler.upload_png("key", "pre-p0.png", b"png")
        self.assertEqual(caught.exception.error_code, "MISTRAL_HTTP_401")
        self.assertEqual(self.post.call_count, 1)

    def test_an_answer_with_no_id_is_a_bad_response(self):
        self.post.return_value = _response(200, {})
        with self.assertRaises(handler.MistralUploadError) as caught:
            handler.upload_png("key", "pre-p0.png", b"png")
        self.assertEqual(caught.exception.error_code, "MISTRAL_BAD_RESPONSE")


class TestDelete(SimpleTestCase):
    """The delete: best effort, a gone file counts, the rest is named."""

    def test_a_delete_lands_on_2xx_and_404(self):
        with mock.patch.object(handler.requests, "delete") as delete:
            delete.return_value = _response(200)
            self.assertTrue(handler.delete_file("key", "f1"))
            delete.return_value = _response(404)
            self.assertTrue(handler.delete_file("key", "f1"))
            delete.return_value = _response(500)
            self.assertFalse(handler.delete_file("key", "f1"))
            delete.side_effect = handler.requests.ConnectionError("down")
            self.assertFalse(handler.delete_file("key", "f1"))

    def test_the_files_no_delete_reached_come_back(self):
        with mock.patch.object(
            handler, "delete_file", side_effect=lambda key, f: f != "b"
        ):
            self.assertEqual(
                handler._delete_files("key", ["a", "b", "c"]), ["b"]
            )
        self.assertEqual(handler._delete_files("key", []), [])
