"""Tests for ``scanning.mistral_client``.

The SDK is mocked at ``mistral_client.client``; nothing here reaches
Mistral. Worth pinning down: the call shapes the ai-research runner
used in production, the status map the sweep rests on, the
transient/terminal split over HTTP statuses, and that a downloaded
line is handed back whole.
"""

import json
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from scanning import mistral_client
from scanning.models import JobStatus

MISTRAL = {"MISTRAL_API_KEY": "key-1", "MISTRAL_MODEL": "mistral-ocr-latest"}


class _StatusError(Exception):
    """A stand-in for the SDK's error, which carries ``status_code``."""

    def __init__(self, status_code: int, message: str = "boom"):
        super().__init__(message)
        self.status_code = status_code


def _sdk_object(**fields):
    """Build a stand-in for an SDK model: attributes plus ``model_dump``."""
    obj = MagicMock()
    for name, value in fields.items():
        setattr(obj, name, value)
    obj.model_dump.return_value = dict(fields)
    return obj


def _client():
    """Return a patcher installing a mock SDK client, and the mock."""
    sdk = MagicMock()
    return patch("scanning.mistral_client.client", return_value=sdk), sdk


class TestEnabled(SimpleTestCase):
    @override_settings(**MISTRAL)
    def test_a_set_key_is_the_switch(self):
        self.assertTrue(mistral_client.enabled())

    @override_settings(**{**MISTRAL, "MISTRAL_API_KEY": ""})
    def test_no_key_means_off(self):
        self.assertFalse(mistral_client.enabled())

    @override_settings(**{**MISTRAL, "MISTRAL_API_KEY": ""})
    def test_the_client_refuses_to_build_without_a_key(self):
        with self.assertRaises(mistral_client.MistralError) as ctx:
            mistral_client.client()
        self.assertEqual(ctx.exception.error_code, "NOT_CONFIGURED")


class TestClassify(SimpleTestCase):
    """The split the retry logic rests on."""

    def test_429_is_busy_and_transient(self):
        error = mistral_client.classify(_StatusError(429))
        self.assertIsInstance(error, mistral_client.MistralBusy)
        self.assertIsInstance(error, mistral_client.MistralTransientError)
        self.assertEqual(error.error_code, "RATE_LIMITED")

    def test_404_is_missing_and_terminal(self):
        error = mistral_client.classify(_StatusError(404))
        self.assertIsInstance(error, mistral_client.MistralMissing)
        self.assertNotIsInstance(error, mistral_client.MistralTransientError)

    def test_5xx_is_transient(self):
        error = mistral_client.classify(_StatusError(503))
        self.assertIsInstance(error, mistral_client.MistralTransientError)
        self.assertNotIsInstance(error, mistral_client.MistralBusy)
        self.assertEqual(error.error_code, "HTTP_503")

    def test_other_4xx_is_terminal(self):
        error = mistral_client.classify(_StatusError(400))
        self.assertIsInstance(error, mistral_client.MistralError)
        self.assertNotIsInstance(error, mistral_client.MistralTransientError)
        self.assertEqual(error.error_code, "HTTP_400")

    def test_no_status_is_a_lost_answer(self):
        error = mistral_client.classify(OSError("connection reset"))
        self.assertIsInstance(error, mistral_client.MistralTransientError)
        self.assertEqual(
            error.error_code, mistral_client.UNANSWERED_ERROR_CODE
        )

    def test_our_own_error_passes_through(self):
        own = mistral_client.MistralBusy("slow down", "RATE_LIMITED", 429)
        self.assertIs(mistral_client.classify(own), own)


@override_settings(**MISTRAL)
class TestCalls(SimpleTestCase):
    """The wire shapes, as the ai-research runner sent them."""

    def test_upload_file_sends_name_content_and_purpose(self):
        patcher, sdk = _client()
        sdk.files.upload.return_value = _sdk_object(id="file-1")
        with patcher:
            file_id = mistral_client.upload_file("p0.png", b"png", "ocr")

        self.assertEqual(file_id, "file-1")
        sdk.files.upload.assert_called_once_with(
            file={"file_name": "p0.png", "content": b"png"}, purpose="ocr"
        )

    def test_create_batch_names_the_ocr_endpoint_and_the_manifest(self):
        patcher, sdk = _client()
        sdk.batch.jobs.create.return_value = _sdk_object(id="batch-1")
        with patcher:
            job_id = mistral_client.create_batch(
                "manifest-1", metadata={"scan": "7"}, timeout_hours=24
            )

        self.assertEqual(job_id, "batch-1")
        sdk.batch.jobs.create.assert_called_once_with(
            endpoint="/v1/ocr",
            input_files=["manifest-1"],
            model="mistral-ocr-latest",
            metadata={"scan": "7"},
            timeout_hours=24,
        )

    def test_a_429_on_an_upload_is_busy(self):
        patcher, sdk = _client()
        sdk.files.upload.side_effect = _StatusError(429)
        with patcher, self.assertRaises(mistral_client.MistralBusy):
            mistral_client.upload_file("p0.png", b"png", "ocr")

    def test_realtime_sends_the_page_as_a_data_uri(self):
        patcher, sdk = _client()
        sdk.ocr.process.return_value = _sdk_object(
            pages=[{"index": 0, "markdown": "x"}], model="mistral-ocr-latest"
        )
        with patcher:
            response = mistral_client.ocr_page_realtime(b"png")

        kwargs = sdk.ocr.process.call_args.kwargs
        self.assertEqual(kwargs["document"]["type"], "image_url")
        self.assertTrue(
            kwargs["document"]["image_url"].startswith(
                "data:image/png;base64,"
            )
        )
        self.assertTrue(kwargs["include_blocks"])
        self.assertEqual(kwargs["table_format"], "html")
        self.assertEqual(response["pages"][0]["markdown"], "x")


@override_settings(**MISTRAL)
class TestPollBatch(SimpleTestCase):
    """Every batch status lands on one JobStatus, and none raises."""

    def _poll(self, **job_fields):
        patcher, sdk = _client()
        sdk.batch.jobs.get.return_value = _sdk_object(**job_fields)
        with patcher:
            return mistral_client.poll_batch("batch-1")

    def test_queued(self):
        outcome = self._poll(id="batch-1", status="QUEUED", total_requests=12)
        self.assertEqual(outcome.status, JobStatus.IN_QUEUE)
        self.assertEqual(outcome.total, 12)
        self.assertEqual(outcome.provider_status, "QUEUED")

    def test_running_carries_the_counts(self):
        outcome = self._poll(
            status="RUNNING",
            total_requests=12,
            succeeded_requests=5,
            failed_requests=1,
        )
        self.assertEqual(outcome.status, JobStatus.IN_PROGRESS)
        self.assertEqual((outcome.succeeded, outcome.failed), (5, 1))

    def test_success_names_the_files_and_keeps_the_job(self):
        outcome = self._poll(
            id="batch-1",
            status="SUCCESS",
            output_file="out-1",
            error_file="err-1",
            total_requests=2,
        )
        self.assertEqual(outcome.status, JobStatus.COMPLETED)
        self.assertEqual(outcome.output_file, "out-1")
        self.assertEqual(outcome.error_file, "err-1")
        self.assertEqual(outcome.job["id"], "batch-1")

    def test_failed_is_retriable(self):
        outcome = self._poll(status="FAILED", errors=[{"message": "oops"}])
        self.assertEqual(outcome.status, JobStatus.FAILED)
        self.assertTrue(outcome.retriable)
        self.assertEqual(outcome.error_code, "BATCH_FAILED")
        self.assertIn("oops", outcome.error_message)

    def test_timeout_is_retriable(self):
        outcome = self._poll(status="TIMEOUT_EXCEEDED")
        self.assertEqual(outcome.status, JobStatus.FAILED)
        self.assertTrue(outcome.retriable)
        self.assertEqual(outcome.error_code, "BATCH_TIMEOUT_EXCEEDED")

    def test_cancelled_upstream_is_terminal(self):
        for status in ("CANCELLED", "CANCELLATION_REQUESTED"):
            outcome = self._poll(status=status)
            self.assertEqual(outcome.status, JobStatus.CANCELLED)
            self.assertFalse(outcome.retriable)
            self.assertEqual(outcome.error_code, "CANCELLED_UPSTREAM")

    def test_an_unknown_status_reads_as_still_at_work(self):
        outcome = self._poll(status="VALIDATING")
        self.assertEqual(outcome.status, JobStatus.IN_PROGRESS)

    def test_a_missing_job_is_expired_and_retriable(self):
        patcher, sdk = _client()
        sdk.batch.jobs.get.side_effect = _StatusError(404)
        with patcher:
            outcome = mistral_client.poll_batch("batch-1")
        self.assertEqual(outcome.status, JobStatus.EXPIRED)
        self.assertTrue(outcome.retriable)
        self.assertEqual(outcome.error_code, "BATCH_MISSING")

    def test_a_failed_poll_learns_nothing(self):
        patcher, sdk = _client()
        sdk.batch.jobs.get.side_effect = _StatusError(503)
        with patcher, self.assertLogs("scanning.mistral_client", "WARNING"):
            outcome = mistral_client.poll_batch("batch-1")
        self.assertIsNone(outcome.status)


@override_settings(**MISTRAL)
class TestDownloadLines(SimpleTestCase):
    """A line comes back whole: every key, nothing renamed."""

    def test_every_key_of_every_line_survives(self):
        lines = [
            {
                "id": "a",
                "custom_id": "p0",
                "response": {
                    "status_code": 200,
                    "body": {
                        "pages": [
                            {
                                "index": 0,
                                "markdown": "# Title",
                                "images": [{"id": "img-0"}],
                                "dimensions": {
                                    "dpi": 200,
                                    "height": 2200,
                                    "width": 1700,
                                },
                                "blocks": [{"type": "text", "content": "x"}],
                            }
                        ],
                        "usage_info": {"pages_processed": 1},
                        "model": "mistral-ocr-2512",
                    },
                },
                "error": None,
            },
            {"id": "b", "custom_id": "p1", "response": None, "error": "bad"},
        ]
        raw = "\n".join(json.dumps(line) for line in lines) + "\n\n"
        response = MagicMock()
        response.read.return_value = raw.encode()
        patcher, sdk = _client()
        sdk.files.download.return_value = response
        with patcher:
            got = mistral_client.download_lines("out-1")

        self.assertEqual(got, lines)
        sdk.files.download.assert_called_once_with(file_id="out-1")

    def test_a_line_that_is_not_json_is_kept_raw(self):
        response = MagicMock()
        response.read.return_value = b'{"custom_id": "p0"}\nnot json\n'
        patcher, sdk = _client()
        sdk.files.download.return_value = response
        with patcher:
            got = mistral_client.download_lines("out-1")
        self.assertEqual(got, [{"custom_id": "p0"}, {"raw": "not json"}])

    def test_bytes_are_accepted_too(self):
        patcher, sdk = _client()
        sdk.files.download.return_value = b'{"custom_id": "p0"}'
        with patcher:
            got = mistral_client.download_lines("out-1")
        self.assertEqual(got, [{"custom_id": "p0"}])


@override_settings(**MISTRAL)
class TestBestEffort(SimpleTestCase):
    """The cancel and the delete never raise: they run inside a sweep."""

    def test_cancel_batch_swallows_a_failure(self):
        patcher, sdk = _client()
        sdk.batch.jobs.cancel.side_effect = _StatusError(500)
        with patcher, self.assertLogs("scanning.mistral_client", "WARNING"):
            mistral_client.cancel_batch("batch-1")
        sdk.batch.jobs.cancel.assert_called_once_with(job_id="batch-1")

    def test_delete_file_swallows_a_failure(self):
        patcher, sdk = _client()
        sdk.files.delete.side_effect = _StatusError(404)
        with patcher, self.assertLogs("scanning.mistral_client", "WARNING"):
            mistral_client.delete_file("file-1")
        sdk.files.delete.assert_called_once_with(file_id="file-1")
