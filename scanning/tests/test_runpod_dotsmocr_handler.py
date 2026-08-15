"""Tests for the standalone RunPod dots.mocr worker handler.

``scanning/runpod-dotsmocr/handler.py`` is a separate deploy artifact:
only that one file is copied into the worker image, and it imports the
worker stack (``runpod``/``openai``/``dots_mocr``) that isn't installed
in the scanning test environment. :func:`_load_handler` injects
lightweight stubs into ``sys.modules`` before importing the module from
its file path, and patches ``shutil.which`` so ``_preload()`` sees no
GPU and returns before trying to spawn a vLLM server.

The resumable-download code is byte-for-byte the one in
``scanning/runpod/handler.py`` and keeps its coverage in
``test_runpod_handler.py``; these tests cover what is new here: the
dispatch/error-code surface, the vLLM fitness gating, the input
validation of the ``parse`` action, and the markdown picture
stripping.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

_HANDLER_PATH = (
    Path(__file__).resolve().parent.parent / "runpod-dotsmocr" / "handler.py"
)


def _load_handler():
    """Import the handler module with its worker-only deps stubbed."""
    stub_runpod = mock.MagicMock()
    # The fitness-check decorator must return the function it wraps so
    # the tests can call the real ``_require_vllm``.
    stub_runpod.serverless.register_fitness_check.side_effect = lambda f: f
    stubs = {"runpod": stub_runpod}
    # ``shutil.which("nvidia-smi")`` -> None makes ``_preload()`` take
    # the no-GPU path regardless of the machine running the tests.
    with (
        mock.patch.dict(sys.modules, stubs),
        mock.patch("shutil.which", return_value=None),
    ):
        spec = importlib.util.spec_from_file_location(
            "_runpod_dotsmocr_handler_under_test", _HANDLER_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


handler = _load_handler()


class _StubAPIError(Exception):
    """Placeholder for the openai exception types the action catches."""


def _stub_dots_mocr_modules():
    """Build sys.modules stubs so ``_action_parse`` can import.

    ``openai`` gets real exception *classes* (a MagicMock attribute is
    not a class and would break the ``except`` clause); the dots_mocr
    consts get real ints so the pixel-bound checks compare cleanly.
    """
    stub_openai = mock.MagicMock()
    stub_openai.APIConnectionError = _StubAPIError
    stub_openai.APITimeoutError = _StubAPIError
    stub_openai.InternalServerError = _StubAPIError

    consts = mock.MagicMock()
    consts.MIN_PIXELS = 3136
    consts.MAX_PIXELS = 11289600

    prompts = mock.MagicMock()
    prompts.dict_promptmode_to_prompt = {
        "prompt_layout_all_en": "layout prompt",
        "prompt_layout_only_en": "layout only prompt",
        "prompt_ocr": "ocr prompt",
    }

    return {
        "openai": stub_openai,
        "dots_mocr": mock.MagicMock(),
        "dots_mocr.utils": mock.MagicMock(),
        "dots_mocr.utils.consts": consts,
        "dots_mocr.utils.doc_utils": mock.MagicMock(),
        "dots_mocr.utils.format_transformer": mock.MagicMock(),
        "dots_mocr.utils.image_utils": mock.MagicMock(),
        "dots_mocr.utils.layout_utils": mock.MagicMock(),
        "dots_mocr.utils.prompts": prompts,
    }


class TestDispatch(SimpleTestCase):
    """The handler's error-code surface, before any real work."""

    def test_missing_action_returns_bad_input(self):
        out = handler.handler({"input": {}})
        self.assertEqual(out["error_code"], "BAD_INPUT")
        self.assertIn("worker_boot_ms", out)
        self.assertIn("worker_uptime_ms", out)
        self.assertFalse(out["gpu_available"])

    def test_non_string_action_returns_bad_input(self):
        out = handler.handler({"input": {"action": 42}})
        self.assertEqual(out["error_code"], "BAD_INPUT")

    def test_no_gpu_returns_no_gpu(self):
        out = handler.handler(
            {"input": {"action": "parse", "pdf_url": "https://x/y.pdf"}}
        )
        self.assertEqual(out["error_code"], "NO_GPU")

    def test_gpu_but_vllm_down_returns_vllm_unhealthy(self):
        with mock.patch.object(handler, "_GPU_AVAILABLE", True):
            out = handler.handler(
                {"input": {"action": "parse", "pdf_url": "https://x/y.pdf"}}
            )
        self.assertEqual(out["error_code"], "VLLM_UNHEALTHY")

    def test_unknown_action(self):
        with (
            mock.patch.object(handler, "_GPU_AVAILABLE", True),
            mock.patch.object(handler, "_VLLM_READY", True),
            mock.patch.object(handler, "_vllm_healthy", return_value=True),
        ):
            out = handler.handler({"input": {"action": "detect"}})
        self.assertEqual(out["error_code"], "UNKNOWN_ACTION")


class TestFitnessCheck(SimpleTestCase):
    """The worker must refuse to join the pool when unfit."""

    def test_raises_without_gpu(self):
        with self.assertRaisesMessage(RuntimeError, "GPU not available"):
            handler._require_vllm()

    def test_raises_with_gpu_but_no_vllm(self):
        with mock.patch.object(handler, "_GPU_AVAILABLE", True):
            with self.assertRaisesMessage(RuntimeError, "vLLM server"):
                handler._require_vllm()

    def test_passes_when_ready(self):
        with (
            mock.patch.object(handler, "_GPU_AVAILABLE", True),
            mock.patch.object(handler, "_VLLM_READY", True),
        ):
            self.assertIsNone(handler._require_vllm())


class TestParseInputValidation(SimpleTestCase):
    """``parse`` rejects bad inputs before downloading anything."""

    def _call_parse(self, inputs, tmp_dir=Path("/nonexistent")):
        with mock.patch.dict(sys.modules, _stub_dots_mocr_modules()):
            return handler._action_parse({"id": "job-1"}, inputs, tmp_dir)

    def test_unsupported_prompt_mode_raises(self):
        with self.assertRaisesMessage(ValueError, "unsupported prompt_mode"):
            self._call_parse(
                {
                    "pdf_url": "https://x/y.pdf",
                    "prompt_mode": "prompt_image_to_svg",
                }
            )

    def test_min_pixels_below_floor_raises(self):
        with self.assertRaisesMessage(ValueError, "min_pixels"):
            self._call_parse({"pdf_url": "https://x/y.pdf", "min_pixels": 100})

    def test_max_pixels_above_ceiling_raises(self):
        with self.assertRaisesMessage(ValueError, "max_pixels"):
            self._call_parse(
                {"pdf_url": "https://x/y.pdf", "max_pixels": 99999999999}
            )

    def test_non_http_pdf_url_raises(self):
        # Reaches _download_pdf, whose scheme check fires before any
        # network I/O.
        with self.assertRaisesMessage(ValueError, "non-http(s)"):
            self._call_parse({"pdf_url": "file:///etc/passwd"})

    def test_string_pixel_bounds_are_range_checked(self):
        # A JSON-encoded number has to fail the same way a real one
        # does. Before these were coerced, the check passed on the
        # string and the bad value only surfaced deep inside every
        # page's fetch_image call.
        for field, value in (
            ("min_pixels", "100"),
            ("max_pixels", "99999999999"),
        ):
            with self.subTest(field=field):
                with self.assertRaisesMessage(ValueError, field):
                    self._call_parse(
                        {"pdf_url": "https://x/y.pdf", field: value}
                    )

    def test_string_pixel_bounds_reach_the_renderer_as_numbers(self):
        # Passing the range check is not enough. Both values are handed
        # to fetch_image, which does arithmetic on them, so a string
        # that survives validation fails inside every page instead and
        # surfaces as "all N pages failed".
        stubs = _stub_dots_mocr_modules()
        fetch_image = stubs["dots_mocr.utils.image_utils"].fetch_image
        # ``_action_parse`` imports fitz itself, so it has to be stubbed
        # in sys.modules rather than patched onto the handler.
        stubs["fitz"] = mock.MagicMock()

        with (
            mock.patch.dict(sys.modules, stubs),
            mock.patch.object(handler, "_download_pdf"),
            mock.patch.object(handler, "_validate_pdf", return_value=1),
        ):
            # Every page fails once inference is reached, which is fine:
            # fetch_image has already been called by then.
            with self.assertRaises(RuntimeError):
                handler._action_parse(
                    {"id": "job-1"},
                    {
                        "pdf_url": "https://x/y.pdf",
                        "min_pixels": "4096",
                        "max_pixels": "1000000",
                    },
                    Path("/nonexistent"),
                )

        kwargs = fetch_image.call_args.kwargs
        self.assertEqual(kwargs["min_pixels"], 4096)
        self.assertEqual(kwargs["max_pixels"], 1000000)
        self.assertIsInstance(kwargs["min_pixels"], int)
        self.assertIsInstance(kwargs["max_pixels"], int)


class TestStripDataUris(SimpleTestCase):
    """Base64 picture crops are stripped from markdown by default."""

    def test_strips_inline_images(self):
        md = (
            "# Title\n\n"
            "![](data:image/png;base64,iVBORw0KGgo=)\n\n"
            "Some text.\n"
            "![alt text](data:image/jpeg;base64,AAAA)\n"
        )
        out = handler._strip_data_uris(md)
        self.assertNotIn("data:image", out)
        self.assertIn("![]()", out)
        self.assertIn("Some text.", out)

    def test_leaves_normal_links_alone(self):
        md = "![figure](https://example.com/fig.png) and [a link](x)."
        self.assertEqual(handler._strip_data_uris(md), md)
