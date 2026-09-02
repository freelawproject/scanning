"""Tests for the standalone RunPod dots.mocr worker handler.

``scanning/runpod-dotsmocr/handler.py`` is a separate deploy artifact:
only that one file is copied into the worker image, and it imports the
worker stack (``runpod``/``openai``/``dots_mocr``) that isn't installed
in the scanning test environment. :func:`_load_handler` injects
lightweight stubs into ``sys.modules`` before importing the module from
its file path, and patches ``shutil.which`` so ``_preload()`` sees no
GPU and returns before trying to spawn a vLLM server.

The handler imports the shared transfer code as a top-level
``runpod_common`` module (the Dockerfile copies it next to handler.py),
so the loader aliases the real ``scanning.runpod_common`` under that
name; its download/validation behaviour is covered in
``test_runpod_common.py``. These tests cover what is specific to this
worker: the dispatch/error-code surface, the vLLM fitness gating, the
input validation of the ``parse`` action, the per-page inference
behaviour (empty-response retry, ``finish_reason='length'`` as a page
failure, the 72-dpi render-fallback flag), and the markdown picture
stripping.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from scanning import runpod_common

_HANDLER_PATH = (
    Path(__file__).resolve().parent.parent / "runpod-dotsmocr" / "handler.py"
)


def _load_handler():
    """Import the handler module with its worker-only deps stubbed."""
    stub_runpod = mock.MagicMock()
    # The fitness-check decorator must return the function it wraps so
    # the tests can call the real ``_require_vllm``.
    stub_runpod.serverless.register_fitness_check.side_effect = lambda f: f
    stubs = {
        "runpod": stub_runpod,
        # The real shared module, under the top-level name the worker
        # image gives it.
        "runpod_common": runpod_common,
    }
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


def _renderer_stubs(origin_size=(1700, 2200)):
    """Stubs configured so ``_parse_page`` runs on real numbers.

    ``_parse_page`` compares the rendered size against what the page
    rect should produce at the requested dpi (the 72-dpi fallback
    check), so the fitz page and the rendered image need concrete
    dimensions: US Letter (612x792 pt), which at the default dpi=200
    renders to 1700x2200 — the default ``origin_size``. Pass a
    different ``origin_size`` to simulate upstream's silent 72-dpi
    re-render. ``_action_parse`` imports fitz itself, so it is stubbed
    in sys.modules rather than patched onto the handler.
    """
    stubs = _stub_dots_mocr_modules()
    page = mock.MagicMock()
    page.rect.width = 612.0
    page.rect.height = 792.0
    fitz = mock.MagicMock()
    fitz.open.return_value.__getitem__.return_value = page
    stubs["fitz"] = fitz
    # A MagicMock, not a SimpleNamespace: the retry ladder's threshold
    # render calls ``.convert`` and ``.point`` on it.
    render = mock.MagicMock()
    render.width, render.height = origin_size
    stubs["dots_mocr.utils.doc_utils"].fitz_doc_to_image.return_value = render
    stubs["dots_mocr.utils.image_utils"].smart_resize.return_value = (
        2212,
        1708,
    )
    return stubs


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
        # A CPU-only worker never grows a GPU: the SDK must terminate
        # it after the response instead of keeping it warm.
        self.assertIs(out["refresh_worker"], True)

    def test_gpu_but_vllm_down_returns_vllm_unhealthy(self):
        with mock.patch.object(handler, "_GPU_AVAILABLE", True):
            out = handler.handler(
                {"input": {"action": "parse", "pdf_url": "https://x/y.pdf"}}
            )
        self.assertEqual(out["error_code"], "VLLM_UNHEALTHY")
        # A crashed vLLM never restarts; without this flag the warm
        # worker bounces every re-queued job in a livelock.
        self.assertIs(out["refresh_worker"], True)

    def test_missing_pdf_url_returns_bad_input(self):
        # Through the full dispatch: _action_parse raises ValueError,
        # handler() converts it to a structured BAD_INPUT so the daemon
        # can classify it as terminal (a bare KeyError traceback would
        # carry no error_code).
        with (
            mock.patch.object(handler, "_GPU_AVAILABLE", True),
            mock.patch.object(handler, "_VLLM_READY", True),
            mock.patch.object(handler, "_vllm_healthy", return_value=True),
            mock.patch.dict(sys.modules, _stub_dots_mocr_modules()),
        ):
            out = handler.handler({"input": {"action": "parse"}})
        self.assertEqual(out["error_code"], "BAD_INPUT")
        self.assertIn("pdf_url", out["error"])

    def test_validation_errors_become_bad_input(self):
        # Any ValueError out of the action (bad prompt_mode here) must
        # come back structured, not as a raised traceback.
        with (
            mock.patch.object(handler, "_GPU_AVAILABLE", True),
            mock.patch.object(handler, "_VLLM_READY", True),
            mock.patch.object(handler, "_vllm_healthy", return_value=True),
            mock.patch.dict(sys.modules, _stub_dots_mocr_modules()),
        ):
            out = handler.handler(
                {
                    "input": {
                        "action": "parse",
                        "pdf_url": "https://x/y.pdf",
                        "prompt_mode": "prompt_image_to_svg",
                    }
                }
            )
        self.assertEqual(out["error_code"], "BAD_INPUT")
        self.assertIn("prompt_mode", out["error"])

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

    def test_over_max_pages_raises(self):
        # The env-level hard guard. There is deliberately no per-job
        # max_pages knob (see the tunables comment in handler.py), so
        # the only limit is MAX_PAGES — and crossing it is a ValueError
        # that handler() turns into a structured BAD_INPUT.
        with (
            mock.patch.dict(sys.modules, _stub_dots_mocr_modules()),
            mock.patch.object(handler, "download_pdf"),
            mock.patch.object(handler, "validate_pdf", return_value=5),
            mock.patch.object(handler, "MAX_PAGES", 3),
        ):
            with self.assertRaisesMessage(ValueError, "exceeds MAX_PAGES=3"):
                handler._action_parse(
                    {"id": "job-1"},
                    {"pdf_url": "https://x/y.pdf"},
                    Path("/nonexistent"),
                )

    def test_string_pixel_bounds_reach_the_renderer_as_numbers(self):
        # Passing the range check is not enough. Both values are handed
        # to fetch_image, which does arithmetic on them, so a string
        # that survives validation fails inside every page instead and
        # surfaces as "all N pages failed".
        stubs = _renderer_stubs()
        fetch_image = stubs["dots_mocr.utils.image_utils"].fetch_image

        with (
            mock.patch.dict(sys.modules, stubs),
            mock.patch.object(handler, "download_pdf"),
            mock.patch.object(handler, "validate_pdf", return_value=1),
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


class TestParsePageInference(SimpleTestCase):
    """Per-page inference behaviour: retries, truncation, render flag.

    Uses ``prompt_ocr`` (no layout post-processing) and a single
    worker thread so a ``side_effect`` list maps to pages in order.
    """

    LOOP = ("x" * 50, "length", 6144)

    def _run(
        self,
        vllm_side_effect,
        pages=1,
        origin_size=(1700, 2200),
        prompt_mode="prompt_ocr",
        post_process=None,
    ):
        stubs = _renderer_stubs(origin_size)
        if post_process is not None:
            stubs[
                "dots_mocr.utils.layout_utils"
            ].post_process_output.side_effect = post_process
        self.stubs = stubs
        with (
            mock.patch.dict(sys.modules, stubs),
            mock.patch.object(handler, "download_pdf"),
            mock.patch.object(handler, "validate_pdf", return_value=pages),
            mock.patch.object(
                handler, "_vllm_inference", side_effect=vllm_side_effect
            ) as infer,
        ):
            result = handler._action_parse(
                {"id": "job-1"},
                {
                    "pdf_url": "https://x/y.pdf",
                    "prompt_mode": prompt_mode,
                    "num_threads": 1,
                },
                Path("/nonexistent"),
            )
        return result, infer

    def test_page_carries_dims_and_token_count(self):
        result, infer = self._run([("some text", "stop", 42)])
        page = result["pages"][0]
        self.assertEqual(page["md"], "some text")
        self.assertEqual(page["completion_tokens"], 42)
        self.assertEqual(page["origin_width"], 1700)
        self.assertEqual(page["origin_height"], 2200)
        self.assertEqual(page["input_width"], 1708)
        self.assertEqual(page["input_height"], 2212)
        self.assertNotIn("render_fallback", page)
        self.assertEqual(result["failed_pages"], [])
        # A first-try success spends one rung and carries no ladder
        # marks, so a reader can tell it from a recovered page.
        self.assertEqual(page["attempts"], 1)
        self.assertNotIn("recovered_by", page)
        self.assertNotIn("render", page)
        self.assertNotIn("errors", page)
        self.assertEqual(result["recovered_pages"], [])
        # The default cap reaches the model: about twice the longest
        # measured page, not the old 16384.
        self.assertEqual(infer.call_args.kwargs["max_completion_tokens"], 6144)
        self.assertEqual(handler.DEFAULT_MAX_COMPLETION_TOKENS, 6144)

    def test_empty_response_retries_then_succeeds(self):
        # "" used to slip through the ``is not None`` check and come
        # back as a successful page with md="". The retry is inside the
        # rung, so it spends no rung.
        result, infer = self._run([("", "stop", 0), ("text!", "stop", 7)])
        self.assertEqual(infer.call_count, 2)
        page = result["pages"][0]
        self.assertEqual(page["md"], "text!")
        self.assertEqual(page["attempts"], 1)
        self.assertEqual(result["failed_pages"], [])

    def test_persistently_empty_response_fails_the_page(self):
        # Every rung gets its own in-rung retries before the page fails.
        empties = [("", "stop", 0)] * handler.INFERENCE_ATTEMPTS * 2
        result, _ = self._run(empties + [("ok", "stop", 1)], pages=2)
        self.assertEqual(result["failed_pages"], [0])
        self.assertIn("empty response", result["pages"][0]["error"])
        self.assertEqual(result["pages"][0]["attempts"], 2)
        self.assertEqual(result["pages"][1]["md"], "ok")

    def test_a_loop_is_retried_on_the_thresholded_render(self):
        # finish_reason='length' means the cap cut the generation — a
        # repetition loop, in practice on the show-through of a mostly
        # blank page. Rung 2 reads the same page with the show-through
        # thresholded away, still greedy.
        sentinel = mock.MagicMock(name="thresholded")
        with mock.patch.object(
            handler, "_threshold_render", return_value=sentinel
        ) as threshold:
            result, infer = self._run([self.LOOP, ("ok", "stop", 3)])
        page = result["pages"][0]
        self.assertEqual(page["md"], "ok")
        self.assertEqual(page["recovered_by"], 2)
        self.assertEqual(page["attempts"], 2)
        self.assertEqual(page["render"], "threshold")
        self.assertEqual(len(page["errors"]), 1)
        self.assertIn("truncated at 6144 tokens", page["errors"][0])
        self.assertEqual(result["failed_pages"], [])
        self.assertEqual(result["recovered_pages"], [0])
        self.assertEqual(infer.call_count, 2)
        # The second rung fed the thresholded render to the model's
        # image pipeline. The render is the only change: the decoding
        # parameters are the same on both calls.
        threshold.assert_called_once()
        fetch = self.stubs["dots_mocr.utils.image_utils"].fetch_image
        self.assertIs(fetch.call_args_list[1].args[0], sentinel)
        first, second = (c.kwargs for c in infer.call_args_list)
        self.assertEqual(first, second)
        self.assertEqual(second["temperature"], 0.0)
        self.assertEqual(second["top_p"], 1.0)

    def test_two_loops_fail_the_page_with_its_history(self):
        result, infer = self._run(
            [self.LOOP, self.LOOP, ("ok", "stop", 3)], pages=2
        )
        page = result["pages"][0]
        self.assertEqual(result["failed_pages"], [0])
        # ``error`` keeps the shape every reader knows: the last text.
        self.assertIn("truncated at 6144 tokens", page["error"])
        self.assertEqual(page["attempts"], 2)
        self.assertEqual(len(page["errors"]), 2)
        self.assertNotIn("recovered_by", page)
        self.assertEqual(result["pages"][1]["md"], "ok")
        self.assertEqual(result["recovered_pages"], [])
        self.assertEqual(infer.call_count, 3)

    def test_a_filtered_answer_takes_a_rung(self):
        # Not-JSON output has no cells and so no page number; it is
        # worth the same retry as a loop, and a rung that answers JSON
        # replaces it.
        result, _ = self._run(
            [("junk", "stop", 5), ("[]", "stop", 6)],
            prompt_mode="prompt_layout_only_en",
            post_process=[
                ("junk text", True),
                ([{"bbox": [0, 0, 1, 1]}], False),
            ],
        )
        page = result["pages"][0]
        self.assertIs(page["filtered"], False)
        self.assertEqual(page["cells"], [{"bbox": [0, 0, 1, 1]}])
        self.assertEqual(page["recovered_by"], 2)
        self.assertEqual(page["errors"], ["model output was not layout JSON"])
        self.assertEqual(result["recovered_pages"], [0])

    def test_a_page_filtered_on_every_rung_is_not_an_error(self):
        # The cleaned text is still text a reader can search, so the
        # page keeps today's shape and stays out of ``failed_pages``.
        result, infer = self._run(
            [("junk", "stop", 5)] * 2,
            prompt_mode="prompt_layout_only_en",
            post_process=[("junk text", True)] * 2,
        )
        page = result["pages"][0]
        self.assertIs(page["filtered"], True)
        self.assertIsNone(page["cells"])
        self.assertEqual(page["md"], "junk text")
        self.assertEqual(page["attempts"], 2)
        self.assertEqual(len(page["errors"]), 2)
        self.assertNotIn("recovered_by", page)
        self.assertEqual(result["failed_pages"], [])
        self.assertEqual(result["recovered_pages"], [])
        self.assertEqual(infer.call_count, 2)

    def test_an_empty_render_takes_no_rung(self):
        # The render is the input to every rung; a re-render of the same
        # page gives the same nothing, so no inference is spent on it.
        result, infer = self._run_empty_render()
        page = result["pages"][0]
        self.assertEqual(page["error"], "page rendered empty")
        self.assertEqual(page["attempts"], 1)
        self.assertEqual(result["failed_pages"], [0])
        self.assertEqual(infer.call_count, 1)

    def _run_empty_render(self):
        stubs = _renderer_stubs()
        stubs["dots_mocr.utils.doc_utils"].fitz_doc_to_image.side_effect = [
            None,
            stubs["dots_mocr.utils.doc_utils"].fitz_doc_to_image.return_value,
        ]
        with (
            mock.patch.dict(sys.modules, stubs),
            mock.patch.object(handler, "download_pdf"),
            mock.patch.object(handler, "validate_pdf", return_value=2),
            mock.patch.object(
                handler, "_vllm_inference", side_effect=[("ok", "stop", 1)]
            ) as infer,
        ):
            result = handler._action_parse(
                {"id": "job-1"},
                {
                    "pdf_url": "https://x/y.pdf",
                    "prompt_mode": "prompt_ocr",
                    "num_threads": 1,
                },
                Path("/nonexistent"),
            )
        return result, infer

    def test_silent_72dpi_rerender_is_flagged(self):
        # Upstream re-renders any page over 4500 px at 72 dpi with no
        # signal; the page must carry the actual render dims and the
        # fallback flag so downstream can rescale its bboxes.
        result, _ = self._run([("ok", "stop", 1)], origin_size=(612, 792))
        page = result["pages"][0]
        self.assertIs(page["render_fallback"], True)
        self.assertEqual(page["origin_width"], 612)
        self.assertEqual(page["origin_height"], 792)
        self.assertEqual(result["failed_pages"], [])


class TestThresholdRender(SimpleTestCase):
    """The retry render: a grey cut that removes the verso show-through."""

    def test_pixels_above_the_threshold_go_white_and_the_rest_black(self):
        from PIL import Image

        image = Image.new("L", (3, 1))
        image.putdata([0, handler.RETRY_THRESHOLD, 255])

        out = handler._threshold_render(image)

        self.assertEqual(out.mode, "RGB")
        self.assertEqual(out.size, image.size)
        self.assertEqual(
            list(out.getdata()), [(0, 0, 0), (0, 0, 0), (255, 255, 255)]
        )
