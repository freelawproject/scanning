"""Tests for the standalone RunPod YOLO worker handler.

``scanning/runpod/handler.py`` is a separate deploy artifact: only that
one file is copied into the worker image, and it imports the worker
stack (``runpod``/``torch``/``ultralytics``/``blackletter``) that isn't
installed in the scanning test environment. :func:`_load_handler`
injects lightweight stubs into ``sys.modules`` before importing the
module from its file path, and gives ``torch`` a CUDA-less answer so
``_preload()`` returns before it tries to open a weight file.

The handler imports the shared transfer code as a top-level
``runpod_common`` module (the Dockerfile copies it next to handler.py),
so the loader aliases the real ``scanning.runpod_common`` under that
name; its download/validation behaviour is covered in
``test_runpod_common.py``. These tests cover what is specific to this
worker: the dispatch and error-code surface, the GPU gating, the input
validation of the ``detect`` action, both delivery shapes, and that the
detection provenance survives into the payload.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from scanning import runpod_common

_HANDLER_PATH = (
    Path(__file__).resolve().parent.parent / "runpod" / "handler.py"
)

#: One merged detection row, shaped as ``blackletter.api.detect``
#: returns it: ``model`` replaced by the ``found_by`` provenance that
#: ``bl_warm.rows_are_bl_warm`` reads downstream (issue #196).
DETECTION_ROW = {
    "page_index": 0,
    "label": "CASE_CAPTION",
    "label_id": 7,
    "confidence": 0.91,
    "bbox": [10.0, 20.0, 300.0, 90.0],
    "img_width": 1700,
    "img_height": 2200,
    "found_by": [{"model": "bl_warm", "confidence": 0.91}],
    "model_count": 1,
}


def _load_handler():
    """Import the handler module with its worker-only deps stubbed."""
    stub_runpod = mock.MagicMock()
    # The fitness-check decorator must return the function it wraps so
    # the tests can call the real ``_require_gpu``.
    stub_runpod.serverless.register_fitness_check.side_effect = lambda f: f
    # ``torch.cuda.is_available()`` -> False makes ``_preload()`` take
    # the no-GPU path regardless of the machine running the tests, so
    # the import opens no weight file.
    stub_torch = mock.MagicMock()
    stub_torch.__version__ = "2.13.0+cu126"
    stub_torch.cuda.is_available.return_value = False
    stubs = {
        "runpod": stub_runpod,
        "torch": stub_torch,
        # The real shared module, under the top-level name the worker
        # image gives it.
        "runpod_common": runpod_common,
    }
    with mock.patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location(
            "_runpod_yolo_handler_under_test", _HANDLER_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


handler = _load_handler()


def _weights(tmp_path: Path, *names: str) -> Path:
    """Create empty weight files and return their directory."""
    for name in names or ("bl_warm",):
        (tmp_path / f"{name}.pt").write_bytes(b"")
    return tmp_path


def _detect_stub(rows=None):
    """Return a ``blackletter.api`` stub whose ``detect`` answers rows."""
    api = mock.MagicMock()
    api.detect.return_value = list(
        rows if rows is not None else [DETECTION_ROW]
    )
    return {"blackletter": mock.MagicMock(), "blackletter.api": api}


class _DetectCase(SimpleTestCase):
    """Shared plumbing: a GPU, a weights directory, and no real IO."""

    def setUp(self):
        super().setUp()
        self.weights_dir = _weights(Path(self.enterContext(_tmp_dir())))
        self.api_stubs = _detect_stub()
        self.enterContext(mock.patch.object(handler, "_CUDA_AVAILABLE", True))
        self.enterContext(
            mock.patch.object(
                handler, "_weights_dir", return_value=self.weights_dir
            )
        )
        self.enterContext(mock.patch.object(handler, "download_pdf"))
        self.enterContext(
            mock.patch.object(handler, "validate_pdf", return_value=3)
        )
        self.enterContext(mock.patch.dict(sys.modules, self.api_stubs))

    @property
    def detect(self):
        return self.api_stubs["blackletter.api"].detect

    def run_detect(self, **inputs):
        """Dispatch one detect job, with the required keys filled in."""
        payload = {"action": "detect", "pdf_url": "https://s3/shard.pdf"}
        payload.update(inputs)
        return handler.handler({"id": "job-1", "input": payload})


def _tmp_dir():
    """Return a TemporaryDirectory context manager."""
    import tempfile

    return tempfile.TemporaryDirectory()


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
            {"input": {"action": "detect", "pdf_url": "https://x/y.pdf"}}
        )
        self.assertEqual(out["error_code"], "NO_GPU")
        # A CPU-only worker never grows a GPU: the SDK must terminate
        # it after the response instead of keeping it warm.
        self.assertIs(out["refresh_worker"], True)

    def test_unknown_action_returns_unknown_action(self):
        with mock.patch.object(handler, "_CUDA_AVAILABLE", True):
            out = handler.handler({"input": {"action": "analyze"}})
        self.assertEqual(out["error_code"], "UNKNOWN_ACTION")
        # The action that went with the legacy pipeline (#173) must
        # read as unknown, not as a crash.
        self.assertIn("detect", out["error"])

    def test_fitness_check_rejects_a_cpu_worker(self):
        with self.assertRaises(RuntimeError):
            handler._require_gpu()


class TestDetectValidation(_DetectCase):
    """Every bad input comes back as a structured BAD_INPUT."""

    def test_missing_pdf_url(self):
        # Through the full dispatch: _action_detect raises ValueError
        # and handler() converts it, so the caller can classify the
        # failure as terminal. A bare KeyError traceback would carry no
        # error_code at all.
        out = handler.handler({"input": {"action": "detect"}})
        self.assertEqual(out["error_code"], "BAD_INPUT")
        self.assertIn("pdf_url", out["error"])

    def test_weight_not_in_the_image_is_refused(self):
        out = self.run_detect(models=["large"])
        self.assertEqual(out["error_code"], "BAD_INPUT")
        self.assertIn("large", out["error"])
        # The message names what the image does carry, so triage needs
        # no shell on the worker.
        self.assertIn("bl_warm", out["error"])
        # Refused before any download: ensure_weights would otherwise
        # reach Hugging Face from inside a paid job.
        self.detect.assert_not_called()
        handler.download_pdf.assert_not_called()

    def test_models_may_be_a_bare_string(self):
        out = self.run_detect(models="bl_warm")
        self.assertEqual(out["models"], ["bl_warm"])

    def test_models_of_the_wrong_type_is_refused(self):
        out = self.run_detect(models=[7])
        self.assertEqual(out["error_code"], "BAD_INPUT")
        self.assertIn("models", out["error"])

    def test_confidence_out_of_range_is_refused(self):
        out = self.run_detect(confidence=1.5)
        self.assertEqual(out["error_code"], "BAD_INPUT")
        self.assertIn("confidence", out["error"])
        self.detect.assert_not_called()

    def test_page_count_over_the_guard_is_refused(self):
        with mock.patch.object(
            handler, "validate_pdf", return_value=handler.MAX_PAGES + 1
        ):
            out = self.run_detect()
        self.assertEqual(out["error_code"], "BAD_INPUT")
        self.assertIn("MAX_PAGES", out["error"])
        self.detect.assert_not_called()

    def test_defaults_reach_blackletter(self):
        self.run_detect()
        _, kwargs = self.detect.call_args
        self.assertEqual(kwargs["models"], ["bl_warm"])
        self.assertEqual(kwargs["confidence"], handler.DEFAULT_CONFIDENCE)
        # No imgsz and no dpi: the checkpoint owns the first and
        # blackletter owns the second.
        self.assertNotIn("imgsz", kwargs)
        self.assertNotIn("dpi", kwargs)


class TestDelivery(_DetectCase):
    """Inline and S3 delivery, and what each response carries."""

    def test_inline_when_no_result_url(self):
        out = self.run_detect()
        self.assertEqual(out["detections"], [DETECTION_ROW])
        self.assertEqual(out["page_count"], 3)
        self.assertEqual(out["models"], ["bl_warm"])
        self.assertIn("duration_ms", out)

    def test_provenance_survives_into_the_payload(self):
        # #196 picks the bl-warm confidence gates off this provenance,
        # so the worker must not strip it.
        out = self.run_detect()
        self.assertEqual(
            out["detections"][0]["found_by"],
            [{"model": "bl_warm", "confidence": 0.91}],
        )

    def test_s3_delivery_returns_a_summary_only(self):
        with mock.patch.object(
            handler, "upload_result", return_value=4096
        ) as upload:
            out = self.run_detect(
                result_url="https://s3/put", result_key="jobs/r/0001/1/d.json"
            )
        # The whole point of the presigned PUT: the rows do not ride
        # back through a response capped at about 20 MB.
        self.assertNotIn("detections", out)
        self.assertEqual(out["detection_count"], 1)
        self.assertEqual(out["result_key"], "jobs/r/0001/1/d.json")
        self.assertEqual(out["bytes"], 4096)
        self.assertEqual(out["page_count"], 3)

        url, envelope, content_type = upload.call_args[0]
        self.assertEqual(url, "https://s3/put")
        self.assertEqual(content_type, "application/json")
        self.assertEqual(
            envelope["schema_version"], handler.RESULT_SCHEMA_VERSION
        )
        self.assertEqual(envelope["action"], "detect")
        self.assertEqual(envelope["result_key"], "jobs/r/0001/1/d.json")
        self.assertEqual(envelope["payload"]["detections"], [DETECTION_ROW])
        # The envelope must survive a round trip as JSON, since that is
        # how the caller reads it back out of the bucket.
        json.dumps(envelope)

    def test_upload_failure_returns_its_own_code(self):
        error = runpod_common.ResultUploadError(
            "expired", "RESULT_URL_EXPIRED"
        )
        with mock.patch.object(handler, "upload_result", side_effect=error):
            out = self.run_detect(
                result_url="https://s3/put", result_key="jobs/r/0001/1/d.json"
            )
        self.assertEqual(out["error_code"], "RESULT_URL_EXPIRED")

    def test_corrupt_download_is_transient_not_bad_input(self):
        with mock.patch.object(
            handler,
            "download_pdf",
            side_effect=runpod_common.CorruptDownloadError("truncated"),
        ):
            out = self.run_detect()
        # The shard in the bucket was verified against the original, so
        # a copy that will not open describes the transfer. BAD_INPUT
        # here would write a volume off for a dropped connection.
        self.assertEqual(out["error_code"], "INPUT_DOWNLOAD_CORRUPT")

    def test_progress_failure_does_not_fail_the_job(self):
        handler.runpod.serverless.progress_update.side_effect = RuntimeError(
            "no job context"
        )
        try:
            out = self.run_detect()
        finally:
            handler.runpod.serverless.progress_update.side_effect = None
        self.assertEqual(out["page_count"], 3)


class TestPreload(SimpleTestCase):
    """The cold-start path, which must never stop the worker starting."""

    def test_no_cuda_skips_the_weight_load(self):
        # The module-level preload already ran this path; assert the
        # flag it leaves behind, which every response reports and the
        # fitness check reads.
        self.assertFalse(handler._CUDA_AVAILABLE)

    def test_a_broken_weight_load_is_swallowed(self):
        stub_ultralytics = mock.MagicMock()
        stub_ultralytics.YOLO.side_effect = RuntimeError("corrupt file")
        stub_torch = mock.MagicMock()
        stub_torch.__version__ = "2.13.0+cu126"
        stub_torch.cuda.is_available.return_value = True
        stub_blackletter = SimpleNamespace(
            __file__="/opt/venv/blackletter/__init__.py"
        )
        with (
            mock.patch.dict(
                sys.modules,
                {
                    "torch": stub_torch,
                    "ultralytics": stub_ultralytics,
                    "blackletter": stub_blackletter,
                },
            ),
            mock.patch.object(handler, "_weights_dir") as weights_dir,
        ):
            weights_dir.return_value = Path("/nonexistent")
            handler._preload()
        # A missing or broken weight is logged, not raised: the first
        # job surfaces the real error instead.
        self.assertTrue(handler._CUDA_AVAILABLE)
        # Leave the module as the other tests expect to find it.
        handler._CUDA_AVAILABLE = False
