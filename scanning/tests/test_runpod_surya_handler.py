"""Tests for the standalone RunPod Surya OCR 2 worker handler.

``scanning/runpod-surya/handler.py`` is a separate deploy artifact:
only that one file is copied into the worker image, and it imports the
worker stack (``runpod``/``surya``) that isn't installed in the
scanning test environment. :func:`_load_handler` injects lightweight
stubs into ``sys.modules`` before importing the module from its file
path, and patches ``shutil.which`` so ``_preload()`` sees no GPU and
returns before trying to spawn a vLLM server.

The handler imports the shared transfer code as a top-level
``runpod_common`` module (the Dockerfile copies it next to handler.py),
so the loader aliases the real ``scanning.runpod_common`` under that
name; its download/validation behaviour is covered in
``test_runpod_common.py``. These tests cover what is specific to this
worker: the dispatch/error-code surface, the vLLM fitness gating, the
``SURYA_*`` environment and the serve command the kit's parameters
reach, the input validation of the ``ocr`` action (no decode
parameter passes), the per-page behaviour (surya called with
``full_page=True`` and nothing else, the raw answer recorded, the
block-mode fallback named, the empty read retried, a dead server
aborting the job), the block serialization and both delivery shapes.

``surya`` is stubbed with a fake predictor that answers a script: each
entry is the ``PageOCRResult`` the predictor returns and the model
answers it made along the way, which it feeds through the manager's
``generate`` so the handler's recorder sees them the way it would see
surya's own requests.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase
from PIL import Image

from scanning import runpod_common

_HANDLER_PATH = (
    Path(__file__).resolve().parent.parent / "runpod-surya" / "handler.py"
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
        # image gives it: the Dockerfile copies it next to handler.py.
        "runpod_common": runpod_common,
    }
    # ``shutil.which("nvidia-smi")`` -> None makes ``_preload()`` take
    # the no-GPU path regardless of the machine running the tests.
    # The SURYA_* names are cleared so the module sets them itself.
    surya_env = {
        k: v for k, v in os.environ.items() if not k.startswith("SURYA_")
    }
    with (
        mock.patch.dict(sys.modules, stubs),
        mock.patch.dict(os.environ, surya_env, clear=True),
        mock.patch("shutil.which", return_value=None),
    ):
        spec = importlib.util.spec_from_file_location(
            "_runpod_surya_handler_under_test", _HANDLER_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # The environment the module set, before the patch is undone.
        module.SURYA_ENV = {
            k: v for k, v in os.environ.items() if k.startswith("SURYA_")
        }
    return module


handler = _load_handler()


# ── surya fakes ─────────────────────────────────────────────────────
def _block(
    bbox=(100.0, 200.0, 900.0, 260.0),
    label="Text",
    raw_label=None,
    html="<p>Hello <i>world</i> &amp; friends</p>",
    order=0,
    confidence=0.97,
    skipped=False,
    error=False,
):
    """A ``BlockOCRResult`` look-alike: a polygon, and ``bbox`` from it."""
    x0, y0, x1, y1 = bbox
    return SimpleNamespace(
        polygon=[[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
        bbox=list(bbox),
        confidence=confidence,
        label=label,
        raw_label=label if raw_label is None else raw_label,
        reading_order=order,
        html=html,
        skipped=skipped,
        error=error,
    )


def _result(blocks, size=(1700, 2200)):
    """A ``PageOCRResult`` look-alike."""
    return SimpleNamespace(
        blocks=list(blocks), image_bbox=[0, 0, float(size[0]), float(size[1])]
    )


FULL_PAGE_ANSWER = (
    '<div data-label="Text" data-bbox="59 91 529 118">'
    "<p>Hello <i>world</i> &amp; friends</p></div>"
)


def _answer(prompt_type, raw, tokens=120, error=False, confidence=0.97):
    """One model answer the fake predictor made for a read."""
    return SimpleNamespace(
        prompt_type=prompt_type,
        raw=raw,
        token_count=tokens,
        error=error,
        mean_token_prob=confidence,
    )


def _read(blocks=None, answers=None, size=(1700, 2200), raise_after=None):
    """One scripted read: the result returned, and the answers made.

    ``raise_after`` is an exception the fake predictor raises once the
    answers are made: a read that failed after the model had spoken.
    """
    if blocks is None:
        blocks = [_block()]
    if answers is None:
        answers = [_answer("high_accuracy_bbox", FULL_PAGE_ANSWER)]
    return {
        "result": _result(blocks, size),
        "answers": list(answers),
        "raise_after": raise_after,
    }


_DIV_RE = re.compile(r"<div ([^>]*)>(.*?)</div>", re.S)
_ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')


def _fake_parse_full_page_html(text):
    """surya's parser, for flat answers: one entry per top-level div,
    in either attribute order, a div with a bad bbox skipped, as
    ``parse_full_page_html`` does."""
    if text == "not html at all":
        raise ValueError("no soup")
    out = []
    for attrs, inner in _DIV_RE.findall(text):
        a = dict(_ATTR_RE.findall(attrs))
        parts = (a.get("data-bbox") or "").split()
        if not a.get("data-label") or len(parts) != 4:
            continue
        out.append(
            SimpleNamespace(
                label=a["data-label"],
                bbox=tuple(float(v) for v in parts),
                html=inner,
            )
        )
    return out


def _surya_stubs(script, calls):
    """sys.modules stubs for the surya package the action imports lazily.

    :param script: The reads, in call order. Each is a :func:`_read`
        dict or an exception instance to raise from the call.
    :param calls: A list the fake predictor appends ``(images,
        kwargs)`` to on every call.
    """
    script = list(script)

    class Manager:
        """Stands in for ``SuryaInferenceManager``: the handler's
        recording subclass overrides ``generate`` and calls up."""

        def __init__(self, method=None, lazy=True):
            self.method = method
            self.started = False

        def start(self):
            self.started = True

        def generate(self, batch):
            # The fake predictor hands the scripted answers in as the
            # batch; the manager answers each item with itself, so the
            # ``BatchOutputItem`` fields are the ones scripted.
            return list(batch)

    class Recognizer:
        """Stands in for ``RecognitionPredictor``."""

        def __init__(self, manager):
            self.manager = manager

        def __call__(self, images, layout_results=None, **kwargs):
            calls.append((images, kwargs))
            entry = script.pop(0)
            if isinstance(entry, Exception):
                raise entry
            if entry["answers"]:
                self.manager.generate(entry["answers"])
            if entry.get("raise_after") is not None:
                raise entry["raise_after"]
            return [entry["result"]] * len(images)

    surya = types.ModuleType("surya")
    inference = types.ModuleType("surya.inference")
    inference.SuryaInferenceManager = Manager
    recognition = types.ModuleType("surya.recognition")
    recognition.RecognitionPredictor = Recognizer
    parsers = types.ModuleType("surya.inference.parsers")
    parsers.parse_full_page_html = _fake_parse_full_page_html
    return {
        "surya": surya,
        "surya.inference": inference,
        "surya.inference.parsers": parsers,
        "surya.recognition": recognition,
        # ``_action_ocr`` imports fitz itself; the render is patched on
        # the handler, so the document only needs to be indexable.
        "fitz": mock.MagicMock(),
    }


class _OcrRun:
    """Run ``_action_ocr`` against a scripted surya, one worker thread."""

    def __init__(self, test, script, pages=1, size=(1700, 2200), **inputs):
        self.calls: list = []
        self.stubs = _surya_stubs(script, self.calls)
        payload = {"pdf_url": "https://x/y.pdf", "num_threads": 1}
        payload.update(inputs)
        render = Image.new("RGB", size, "white")
        with (
            mock.patch.dict(sys.modules, self.stubs),
            mock.patch.object(handler, "download_pdf"),
            mock.patch.object(handler, "validate_pdf", return_value=pages),
            mock.patch.object(handler, "_render_page", return_value=render),
            mock.patch.object(handler, "_RECOGNIZER", None),
            mock.patch.object(handler, "_vllm_healthy", return_value=True),
        ):
            self.result = handler._action_ocr(
                {"id": "job-1"}, payload, Path("/nonexistent")
            )


def _vllm_up():
    """Patches that let a job through the health gates of ``handler()``."""
    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch.object(handler, "_GPU_AVAILABLE", True))
    stack.enter_context(mock.patch.object(handler, "_VLLM_READY", True))
    stack.enter_context(
        mock.patch.object(handler, "_vllm_healthy", return_value=True)
    )
    return stack


class TestDispatch(SimpleTestCase):
    """The handler's error-code surface, before any real work."""

    def test_missing_action_returns_bad_input(self):
        out = handler.handler({"input": {}})
        self.assertEqual(out["error_code"], "BAD_INPUT")
        self.assertIn("worker_boot_ms", out)
        self.assertIn("worker_uptime_ms", out)
        self.assertFalse(out["gpu_available"])

    def test_no_gpu_returns_no_gpu(self):
        out = handler.handler(
            {"input": {"action": "ocr", "pdf_url": "https://x/y.pdf"}}
        )
        self.assertEqual(out["error_code"], "NO_GPU")
        # A CPU-only worker never grows a GPU: the SDK must terminate
        # it after the response instead of keeping it warm.
        self.assertIs(out["refresh_worker"], True)

    def test_gpu_but_vllm_down_returns_vllm_unhealthy(self):
        with mock.patch.object(handler, "_GPU_AVAILABLE", True):
            out = handler.handler(
                {"input": {"action": "ocr", "pdf_url": "https://x/y.pdf"}}
            )
        self.assertEqual(out["error_code"], "VLLM_UNHEALTHY")
        self.assertIs(out["refresh_worker"], True)

    def test_missing_pdf_url_returns_bad_input(self):
        # Through the full dispatch: the action raises BadInputError,
        # the runner answers a structured BAD_INPUT the daemon can
        # classify as terminal.
        with (
            _vllm_up(),
            mock.patch.dict(sys.modules, _surya_stubs([], [])),
        ):
            out = handler.handler({"input": {"action": "ocr"}})
        self.assertEqual(out["error_code"], "BAD_INPUT")
        self.assertIn("pdf_url", out["error"])

    def test_a_decode_parameter_is_refused(self):
        # The issue's one hard rule: no temperature reaches the model
        # from here. Refused loudly, not ignored, so a caller learns.
        for name in handler.REFUSED_INPUTS:
            with self.subTest(name=name):
                with (
                    _vllm_up(),
                    mock.patch.dict(sys.modules, _surya_stubs([], [])),
                ):
                    out = handler.handler(
                        {
                            "input": {
                                "action": "ocr",
                                "pdf_url": "https://x/y.pdf",
                                name: 0.2,
                            }
                        }
                    )
                self.assertEqual(out["error_code"], "BAD_INPUT")
                self.assertIn(name, out["error"])
                self.assertIn("decode", out["error"])

    def test_unknown_action(self):
        with _vllm_up():
            out = handler.handler({"input": {"action": "parse"}})
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


class TestSuryaConfiguration(SimpleTestCase):
    """The kit's parameters, and nothing beyond them, reach surya and vLLM."""

    def test_surya_is_pointed_at_the_local_server_before_import(self):
        env = handler.SURYA_ENV
        self.assertEqual(env["SURYA_INFERENCE_BACKEND"], "vllm")
        self.assertEqual(
            env["SURYA_INFERENCE_URL"],
            f"http://127.0.0.1:{handler.VLLM_PORT}/v1",
        )
        self.assertEqual(env["SURYA_MODEL_CHECKPOINT"], handler.SURYA_MODEL)
        self.assertEqual(env["SURYA_INFERENCE_PARALLEL"], "8")
        # No decode setting: surya's own defaults (greedy) stand.
        for name in env:
            self.assertNotIn("TEMPERATURE", name)
            self.assertNotIn("REGEN", name)
            self.assertNotIn("TOKENS", name)

    def test_serve_command_carries_the_kit_flags(self):
        cmd = handler._vllm_command()
        self.assertEqual(cmd[:3], ["vllm", "serve", "datalab-to/surya-ocr-2"])
        flags = {}
        rest = cmd[3:]
        for i, token in enumerate(rest):
            if not token.startswith("--"):
                continue
            after = rest[i + 1] if i + 1 < len(rest) else None
            flags[token] = (
                None if after is None or after.startswith("--") else after
            )
        self.assertEqual(flags["--served-model-name"], handler.SURYA_MODEL)
        self.assertEqual(flags["--dtype"], "bfloat16")
        self.assertEqual(flags["--max-model-len"], "18000")
        self.assertEqual(flags["--gpu-memory-utilization"], "0.85")
        self.assertEqual(
            flags["--mm-processor-kwargs"],
            '{"min_pixels": 3136, "max_pixels": 6291456}',
        )
        self.assertIn("--enable-prefix-caching", cmd)
        # MTP is a throughput switch the kit left off.
        self.assertNotIn("--speculative-config", cmd)
        self.assertNotIn("--trust-remote-code", cmd)

    def test_mtp_is_a_switch(self):
        with mock.patch.object(handler, "VLLM_ENABLE_MTP", True):
            cmd = handler._vllm_command()
        self.assertIn("--speculative-config", cmd)
        self.assertIn(
            '"method": "mtp"', cmd[cmd.index("--speculative-config") + 1]
        )

    def test_the_reserved_port_name_never_reaches_the_server(self):
        with (
            mock.patch.dict(os.environ, {"VLLM_PORT": "9999"}),
            mock.patch.object(handler.subprocess, "Popen") as popen,
        ):
            handler._start_vllm()
        self.assertNotIn("VLLM_PORT", popen.call_args.kwargs["env"])


class TestOcrInputValidation(SimpleTestCase):
    """``ocr`` rejects bad inputs before downloading anything."""

    def _call(self, inputs):
        with mock.patch.dict(sys.modules, _surya_stubs([], [])):
            return handler._action_ocr(
                {"id": "job-1"}, inputs, Path("/nonexistent")
            )

    def test_non_http_pdf_url_raises(self):
        # Reaches download_pdf, whose scheme check fires before any
        # network I/O.
        with self.assertRaisesMessage(ValueError, "non-http(s)"):
            self._call({"pdf_url": "file:///etc/passwd"})

    def test_dpi_is_coerced_and_ranged(self):
        with self.assertRaisesMessage(ValueError, "dpi"):
            self._call({"pdf_url": "https://x/y.pdf", "dpi": "lots"})
        with self.assertRaisesMessage(ValueError, "dpi"):
            self._call({"pdf_url": "https://x/y.pdf", "dpi": 10})

    def test_num_threads_is_bounded(self):
        # Every thread holds a rendered page; a caller cannot ask for
        # hundreds of them.
        for value in (0, handler.MAX_NUM_THREADS + 1):
            with self.subTest(value=value):
                with self.assertRaisesMessage(ValueError, "num_threads"):
                    self._call(
                        {"pdf_url": "https://x/y.pdf", "num_threads": value}
                    )

    def test_default_threads_are_the_measured_sixteen(self):
        self.assertEqual(handler.DEFAULT_NUM_THREADS, 16)
        self.assertLessEqual(
            handler.DEFAULT_NUM_THREADS, handler.MAX_NUM_THREADS
        )

    def test_over_max_pages_raises(self):
        # The env-level hard guard; crossing it is a BadInputError that
        # handler() turns into a structured BAD_INPUT.
        with (
            mock.patch.dict(sys.modules, _surya_stubs([], [])),
            mock.patch.object(handler, "download_pdf"),
            mock.patch.object(handler, "validate_pdf", return_value=5),
            mock.patch.object(handler, "MAX_PAGES", 3),
        ):
            with self.assertRaisesMessage(ValueError, "exceeds MAX_PAGES=3"):
                handler._action_ocr(
                    {"id": "job-1"},
                    {"pdf_url": "https://x/y.pdf"},
                    Path("/nonexistent"),
                )


class TestOcrPages(SimpleTestCase):
    """Per-page behaviour: the call, the record, the retry, the abort."""

    def test_a_page_is_read_whole_and_serialized(self):
        run = _OcrRun(self, [_read()])
        page = run.result["pages"][0]

        # The kit's call: one image, full_page=True, nothing else.
        self.assertEqual(len(run.calls), 1)
        images, kwargs = run.calls[0]
        self.assertEqual(len(images), 1)
        self.assertEqual(kwargs, {"full_page": True})

        self.assertEqual(page["page_no"], 0)
        self.assertEqual(
            (page["origin_width"], page["origin_height"]), (1700, 2200)
        )
        self.assertEqual(len(page["blocks"]), 1)
        block = page["blocks"][0]
        self.assertEqual(block["bbox"], [100.0, 200.0, 900.0, 260.0])
        self.assertEqual(block["label"], "Text")
        self.assertEqual(
            block["html"], "<p>Hello <i>world</i> &amp; friends</p>"
        )
        self.assertEqual(block["text"], "Hello world & friends")
        self.assertEqual(block["confidence"], 0.97)
        self.assertIs(block["skipped"], False)
        self.assertIs(block["error"], False)
        self.assertEqual(page["text"], "Hello world & friends")
        # The answer as the model wrote it travels beside the parse.
        self.assertEqual(page["raw"], FULL_PAGE_ANSWER)
        self.assertEqual(page["requests"], 1)
        self.assertEqual(page["completion_tokens"], 120)
        self.assertEqual(page["confidence"], 0.97)
        self.assertEqual(page["attempts"], 1)
        self.assertNotIn("fallback", page)
        self.assertNotIn("empty", page)
        self.assertNotIn("error_blocks", page)
        self.assertIn("duration_ms", page)
        # The answer parsed to one div and the one block is it.
        self.assertEqual(page["parsed_blocks"], 1)
        self.assertNotIn("dropped_blocks", page)
        self.assertEqual(run.result["page_count"], 1)
        self.assertEqual(run.result["failed_pages"], [])
        self.assertEqual(run.result["empty_pages"], [])
        self.assertEqual(run.result["fallback_pages"], [])
        self.assertEqual(run.result["dropped_block_pages"], [])

    def test_the_bbox_falls_back_to_the_polygon(self):
        block = _block()
        del block.bbox
        run = _OcrRun(self, [_read(blocks=[block])])
        self.assertEqual(
            run.result["pages"][0]["blocks"][0]["bbox"],
            [100.0, 200.0, 900.0, 260.0],
        )

    def test_a_block_mode_fallback_is_named_and_the_looped_answer_kept(self):
        # surya asked again after the full-page answer failed: a layout
        # pass, then one request per block. The page says so, and its
        # ``raw`` is the answer that failed: the evidence of the loop.
        looped = "<div>a</div>" * 40
        answers = [
            _answer(
                "high_accuracy_bbox", looped, tokens=12288, confidence=0.4
            ),
            _answer(
                "layout",
                '[{"label": "Text", "bbox": "59 91 529 118"}]',
                tokens=30,
            ),
            _answer("block", "<p>Hello</p>", tokens=8),
            _answer("block", "", tokens=0, error=True),
        ]
        blocks = [_block(), _block(order=1, html="", error=True)]
        run = _OcrRun(self, [_read(blocks=blocks, answers=answers)])
        page = run.result["pages"][0]
        self.assertEqual(page["fallback"], "block")
        self.assertEqual(page["raw"], looped)
        self.assertEqual(page["requests"], 4)
        self.assertEqual(page["completion_tokens"], 12288 + 30 + 8)
        self.assertEqual(page["error_blocks"], 1)
        self.assertEqual(page["confidence"], 0.4)
        self.assertEqual(run.result["fallback_pages"], [0])
        self.assertEqual(run.result["failed_pages"], [])

    def test_an_empty_read_is_read_again(self):
        # A server can answer no blocks and no exception; the second
        # read is what tells a blank page from a lost one.
        run = _OcrRun(
            self,
            [_read(blocks=[], answers=[]), _read()],
        )
        page = run.result["pages"][0]
        self.assertEqual(len(run.calls), 2)
        self.assertEqual(page["attempts"], 2)
        self.assertEqual(len(page["blocks"]), 1)
        self.assertNotIn("empty", page)
        self.assertEqual(run.result["empty_pages"], [])

    def test_a_page_of_failed_blocks_counts_as_empty(self):
        failed = [
            _block(html="", error=True),
            _block(order=1, html="", error=True),
        ]
        run = _OcrRun(self, [_read(blocks=failed, answers=[]), _read()])
        self.assertEqual(run.result["pages"][0]["attempts"], 2)
        self.assertEqual(len(run.result["pages"][0]["blocks"]), 1)

    def test_a_skipped_figure_is_not_empty(self):
        figure = _block(label="Figure", html="", skipped=True)
        raw = '<div data-bbox="59 91 529 118" data-label="Figure"><img/></div>'
        run = _OcrRun(
            self,
            [
                _read(
                    blocks=[figure],
                    answers=[_answer("high_accuracy_bbox", raw)],
                )
            ],
        )
        page = run.result["pages"][0]
        self.assertEqual(page["attempts"], 1)
        self.assertNotIn("empty", page)
        # surya emptied the block's html; the parsed answer gives it
        # back, which for a figure is the image tag and no text.
        self.assertEqual(page["blocks"][0]["html"], "<img/>")
        self.assertEqual(page["text"], "")
        self.assertIs(page["blocks"][0]["skipped"], True)

    def test_a_complex_block_keeps_its_text_from_raw(self):
        # surya canonicalizes Complex-Block to Figure and skips it: the
        # model's text for it survives in the answer only, so the
        # block gets it back from there. The other block is untouched.
        raw = (
            '<div data-bbox="10 10 500 200" data-label="Complex-Block">'
            "<p>Held: the <b>motion</b> is denied.</p></div>"
            '<div data-bbox="10 210 500 300" data-label="Text"><p>Plain</p></div>'
        )
        blocks = [
            _block(
                label="Figure",
                raw_label="Complex-Block",
                html="",
                skipped=True,
            ),
            _block(order=1, html="<p>Plain</p>"),
        ]
        run = _OcrRun(
            self,
            [
                _read(
                    blocks=blocks, answers=[_answer("high_accuracy_bbox", raw)]
                )
            ],
        )
        page = run.result["pages"][0]
        first, second = page["blocks"]
        self.assertEqual(
            first["html"], "<p>Held: the <b>motion</b> is denied.</p>"
        )
        self.assertEqual(first["text"], "Held: the motion is denied.")
        self.assertIs(first["skipped"], True)
        self.assertEqual(second["html"], "<p>Plain</p>")
        self.assertEqual(page["text"], "Held: the motion is denied.\nPlain")
        self.assertEqual(page["parsed_blocks"], 2)
        self.assertNotIn("dropped_blocks", page)

    def test_a_block_surya_dropped_is_counted_and_named(self):
        # The answer had three divs; surya kept two (a blank-crop drop
        # in the middle keeps the survivors' numbering).
        raw = (
            '<div data-bbox="10 10 500 100" data-label="Text"><p>One</p></div>'
            '<div data-bbox="10 110 500 200" data-label="Text"><p>ghost</p></div>'
            '<div data-bbox="10 210 500 300" data-label="Page-Footer"><p>3</p></div>'
        )
        blocks = [
            _block(order=0, html="<p>One</p>"),
            _block(
                order=2,
                label="PageFooter",
                raw_label="Page-Footer",
                html="<p>3</p>",
            ),
        ]
        run = _OcrRun(
            self,
            [
                _read(
                    blocks=blocks, answers=[_answer("high_accuracy_bbox", raw)]
                )
            ],
            pages=1,
        )
        page = run.result["pages"][0]
        self.assertEqual(page["parsed_blocks"], 3)
        self.assertEqual(
            page["dropped_blocks"], [{"order": 1, "raw_label": "Text"}]
        )
        self.assertEqual(run.result["dropped_block_pages"], [0])

    def test_a_block_mode_page_is_counted_but_not_realigned(self):
        # In block mode the blocks are numbered by the layout pass, not
        # by the failed answer, so nothing is restored or reported as
        # dropped; the count of what the answer held is still kept.
        looped = (
            '<div data-bbox="1 1 2 2" data-label="Figure"><p>x</p></div>' * 3
        )
        answers = [
            _answer("high_accuracy_bbox", looped),
            _answer("layout", "[]"),
            _answer("block", "<p>y</p>"),
        ]
        figure = _block(label="Figure", html="", skipped=True)
        run = _OcrRun(self, [_read(blocks=[figure], answers=answers)])
        page = run.result["pages"][0]
        self.assertEqual(page["fallback"], "block")
        self.assertEqual(page["parsed_blocks"], 3)
        self.assertNotIn("dropped_blocks", page)
        self.assertEqual(page["blocks"][0]["html"], "")

    def test_an_unparseable_answer_leaves_the_count_none(self):
        answers = [_answer("high_accuracy_bbox", "not html at all")]
        run = _OcrRun(self, [_read(answers=answers)])
        page = run.result["pages"][0]
        self.assertIsNone(page["parsed_blocks"])
        self.assertEqual(page["raw"], "not html at all")
        self.assertEqual(len(page["blocks"]), 1)

    def test_a_raising_read_keeps_the_answer(self):
        # The model answered and then the read raised: the answer is
        # the evidence, as on a failed dots.mocr page.
        run = _OcrRun(
            self,
            [_read(raise_after=RuntimeError("parse blew up")), _read()],
            pages=2,
        )
        page = run.result["pages"][0]
        self.assertEqual(page["error"], "parse blew up")
        self.assertEqual(page["raw"], FULL_PAGE_ANSWER)
        self.assertEqual(run.result["failed_pages"], [0])

    def test_twice_empty_is_written_and_marked(self):
        # Kept so the shard converges, marked so a reader checks it.
        run = _OcrRun(
            self,
            [
                _read(blocks=[], answers=[]),
                _read(blocks=[], answers=[]),
                _read(),
            ],
            pages=2,
        )
        page = run.result["pages"][0]
        self.assertIs(page["empty"], True)
        self.assertEqual(page["attempts"], 2)
        self.assertEqual(page["blocks"], [])
        self.assertIsNone(page["raw"])
        self.assertIsNone(page["completion_tokens"])
        self.assertEqual(run.result["empty_pages"], [0])
        self.assertEqual(run.result["failed_pages"], [])
        self.assertEqual(run.result["pages"][1]["attempts"], 1)

    def test_a_raising_read_fails_the_page_and_not_the_job(self):
        run = _OcrRun(self, [ValueError("bad image"), _read()], pages=2)
        page = run.result["pages"][0]
        self.assertEqual(page["error"], "bad image")
        self.assertEqual(page["attempts"], 1)
        self.assertEqual(run.result["failed_pages"], [0])
        self.assertEqual(len(run.result["pages"][1]["blocks"]), 1)

    def test_a_raise_on_the_second_read_reports_two_attempts(self):
        run = _OcrRun(
            self,
            [_read(blocks=[], answers=[]), ValueError("boom"), _read()],
            pages=2,
        )
        page = run.result["pages"][0]
        self.assertEqual(page["error"], "boom")
        self.assertEqual(page["attempts"], 2)
        self.assertEqual(run.result["failed_pages"], [0])

    def test_an_errored_request_leaves_raw_none(self):
        # surya hands an errored request back as raw ""; the page says
        # "no answer" the one way, None.
        answers = [_answer("high_accuracy_bbox", "", tokens=0, error=True)]
        run = _OcrRun(
            self,
            [_read(blocks=[], answers=answers), _read()],
        )
        self.assertEqual(run.result["pages"][0]["attempts"], 2)
        run = _OcrRun(
            self,
            [_read(blocks=[], answers=answers)] * 2 + [_read()],
            pages=2,
        )
        self.assertIsNone(run.result["pages"][0]["raw"])

    def test_pages_come_back_in_order(self):
        script = [_read(blocks=[_block(html=f"<p>{n}</p>")]) for n in range(3)]
        run = _OcrRun(self, script, pages=3)
        self.assertEqual(
            [p["page_no"] for p in run.result["pages"]], [0, 1, 2]
        )
        self.assertEqual(run.result["pages"][2]["text"], "2")

    def test_a_shard_of_nothing_fails_the_job(self):
        # Every page failed or empty is a dead server, not a blank
        # volume: RunPod must mark the job FAILED so the daemon
        # re-queues it, instead of a "success" with no text in it.
        with self.assertRaisesMessage(RuntimeError, "all 2 pages"):
            _OcrRun(
                self,
                [_read(blocks=[], answers=[])] * 2 + [ValueError("boom")],
                pages=2,
            )

    def test_a_streak_of_empties_on_a_dead_server_aborts(self):
        # Sixteen pages in a row came back empty and the server no
        # longer answers: the queued pages are not read. The one worker
        # thread runs ahead of the collector by a page or two, so the
        # count is a bound and not a number.
        script = [_read(blocks=[], answers=[])] * 800
        calls: list = []
        stubs = _surya_stubs(script, calls)
        with (
            mock.patch.dict(sys.modules, stubs),
            mock.patch.object(handler, "download_pdf"),
            mock.patch.object(handler, "validate_pdf", return_value=400),
            mock.patch.object(
                handler,
                "_render_page",
                return_value=Image.new("RGB", (10, 10), "white"),
            ),
            mock.patch.object(handler, "_RECOGNIZER", None),
            mock.patch.object(handler, "_vllm_healthy", return_value=False),
        ):
            with self.assertRaisesMessage(RuntimeError, "stopped answering"):
                handler._action_ocr(
                    {"id": "job-1"},
                    {"pdf_url": "https://x/y.pdf", "num_threads": 1},
                    Path("/nonexistent"),
                )
        # Two reads a page for at least the sixteen, and the queue
        # dropped long before the four hundred.
        self.assertGreaterEqual(len(calls), 2 * handler.ABORT_STREAK)
        self.assertLess(len(calls), 2 * 400)

    def test_a_streak_of_empties_on_a_live_server_goes_on(self):
        # The same streak with a healthy server is a run of blank pages
        # (a stack of separators, say): the job reads to the end.
        script = [_read(blocks=[], answers=[])] * 40 + [_read()]
        run = _OcrRun(self, script, pages=21)
        self.assertEqual(len(run.result["empty_pages"]), 20)
        self.assertEqual(run.result["failed_pages"], [])

    def test_the_predictor_is_built_once(self):
        # The attach probes the server and the served name; it runs on
        # the first job and never again in the process.
        calls: list = []
        stubs = _surya_stubs([_read(), _read()], calls)
        with (
            mock.patch.dict(sys.modules, stubs),
            mock.patch.object(handler, "_RECOGNIZER", None),
            mock.patch.object(
                handler, "_build_recognizer", wraps=handler._build_recognizer
            ) as build,
        ):
            first = handler._recognizer()
            second = handler._recognizer()
        self.assertIs(first, second)
        self.assertEqual(build.call_count, 1)
        self.assertEqual(first.manager.method, "vllm")
        self.assertTrue(first.manager.started)


class TestRequestLog(SimpleTestCase):
    """The recorder notes a thread's own requests and nobody else's."""

    def test_notes_between_open_and_close_only(self):
        log = handler.RequestLog()
        log.note([_answer("layout", "x")], [_answer("layout", "x")])
        self.assertEqual(log.close(), [])
        log.open()
        log.note(
            [_answer("high_accuracy_bbox", "<div/>")],
            [
                _answer(
                    "high_accuracy_bbox", "<div/>", tokens=7, confidence=0.5
                )
            ],
        )
        records = log.close()
        self.assertEqual(
            records,
            [
                {
                    "prompt_type": "high_accuracy_bbox",
                    "raw": "<div/>",
                    "completion_tokens": 7,
                    "error": False,
                    "confidence": 0.5,
                }
            ],
        )
        self.assertEqual(log.close(), [])

    def test_threads_do_not_see_each_other(self):
        import threading

        log = handler.RequestLog()
        seen = {}

        def _worker(name):
            log.open()
            log.note([_answer("block", name)], [_answer("block", name)])
            seen[name] = [r["raw"] for r in log.close()]

        threads = [threading.Thread(target=_worker, args=(n,)) for n in "ab"]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(seen, {"a": ["a"], "b": ["b"]})


class TestHtmlToText(SimpleTestCase):
    def test_tags_go_entities_unescape_breaks_stay(self):
        html = "<p>One&nbsp;two<br>three</p><p>four &lt; five  six</p>"
        self.assertEqual(
            handler._html_to_text(html), "One two\nthree\nfour < five six"
        )

    def test_table_cells_are_boundaries(self):
        # The model writes an author block as one table row, a cell per
        # author; the first smoke run glued "avaswani@google.comNoam".
        html = (
            "<table><tbody><tr><td><b>Ashish</b><br/>a@x.com</td>"
            "<td><b>Noam</b><br/>n@x.com</td></tr></tbody></table><hr/>tail"
        )
        self.assertEqual(
            handler._html_to_text(html), "Ashish\na@x.com\nNoam\nn@x.com\ntail"
        )

    def test_empty(self):
        self.assertEqual(handler._html_to_text(""), "")
        self.assertEqual(handler._html_to_text(None), "")


class TestDelivery(SimpleTestCase):
    """Both shapes of the answer, through the full dispatch."""

    def _job(self, extra):
        stubs = _surya_stubs([_read()], [])
        payload = {"action": "ocr", "scan_pk": 7, "pdf_url": "https://x/y.pdf"}
        payload.update(extra)
        with (
            _vllm_up(),
            mock.patch.dict(sys.modules, stubs),
            mock.patch.object(handler, "download_pdf"),
            mock.patch.object(handler, "validate_pdf", return_value=1),
            mock.patch.object(
                handler,
                "_render_page",
                return_value=Image.new("RGB", (10, 10), "white"),
            ),
            mock.patch.object(handler, "_RECOGNIZER", None),
            mock.patch.object(
                handler, "upload_result", return_value=4321
            ) as up,
        ):
            out = handler.handler({"id": "job-1", "input": payload})
        return out, up

    def test_inline_without_result_url(self):
        out, up = self._job({})
        self.assertEqual(len(out["pages"]), 1)
        self.assertEqual(out["page_count"], 1)
        self.assertIs(out["gpu_available"], True)
        up.assert_not_called()

    def test_summary_with_result_url(self):
        out, up = self._job(
            {"result_url": "https://s3/put", "result_key": "jobs/x.json"}
        )
        self.assertNotIn("pages", out)
        self.assertEqual(out["result_key"], "jobs/x.json")
        self.assertEqual(out["bytes"], 4321)
        for field in handler._SUMMARY_FIELDS:
            self.assertIn(field, out)
        url, envelope, content_type = up.call_args.args
        self.assertEqual(url, "https://s3/put")
        self.assertEqual(content_type, runpod_common.RESULT_CONTENT_TYPE)
        self.assertEqual(envelope["action"], "ocr")
        self.assertEqual(envelope["scan_pk"], 7)
        self.assertEqual(envelope["result_key"], "jobs/x.json")
        self.assertEqual(
            envelope["payload"]["pages"][0]["raw"], FULL_PAGE_ANSWER
        )


class TestSummaryFields(SimpleTestCase):
    """What the response keeps when the payload goes to S3."""

    def test_the_page_lists_travel_and_the_pages_do_not(self):
        for field in (
            "failed_pages",
            "empty_pages",
            "fallback_pages",
            "dropped_block_pages",
        ):
            self.assertIn(field, handler._SUMMARY_FIELDS)
        self.assertNotIn("pages", handler._SUMMARY_FIELDS)
