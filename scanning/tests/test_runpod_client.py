"""Tests for ``scanning.runpod_client``.

Every RunPod HTTP call is mocked at the ``requests`` layer; nothing here
reaches the network or the RunPod API.

The module is transport only, so what matters is the classification: a
lost answer against a refusal, an endpoint that cannot take work against
a job that is bad, and a 404 whose output is nevertheless on S3. Row
lifecycle behaviour lives in ``test_jobs``.
"""

from unittest.mock import MagicMock, patch

import requests
from django.test import TestCase, override_settings

from scanning import runpod_client
from scanning.models import JobStatus

_BASE = "https://api.runpod.ai/v2/ep-1"
_HEADERS = {"Authorization": "Bearer key-1"}


def _mock_response(status_code: int, body: dict | list | None = None):
    """Build a stand-in for a ``requests.Response``.

    :param status_code: HTTP status.
    :param body: Parsed JSON body. ``None`` makes ``.json()`` raise
        ``ValueError``, for the malformed-body tests.
    :returns: A mock that behaves like a Response.
    :rtype: MagicMock
    """
    response = MagicMock()
    response.status_code = status_code
    if body is None:
        response.json.side_effect = ValueError("no json body")
        response.text = ""
    else:
        response.json.return_value = body
        response.text = str(body)
    if status_code >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(
            f"{status_code} error", response=response
        )
    else:
        response.raise_for_status.return_value = None
    return response


# ── Configuration ───────────────────────────────────────────────────
class TestEnabled(TestCase):
    """``enabled`` needs all three switches, and the id is per engine."""

    @override_settings(
        RUNPOD_ENABLED=True, RUNPOD_API_KEY="key-1", DEVELOPMENT=False
    )
    def test_all_three_present(self):
        self.assertTrue(runpod_client.enabled("ep-1"))

    @override_settings(
        RUNPOD_ENABLED=False, RUNPOD_API_KEY="key-1", DEVELOPMENT=False
    )
    def test_master_switch_off(self):
        self.assertFalse(runpod_client.enabled("ep-1"))

    @override_settings(
        RUNPOD_ENABLED=True, RUNPOD_API_KEY="", DEVELOPMENT=False
    )
    def test_no_api_key(self):
        self.assertFalse(runpod_client.enabled("ep-1"))

    @override_settings(
        RUNPOD_ENABLED=True, RUNPOD_API_KEY="key-1", DEVELOPMENT=False
    )
    def test_one_engine_unset_does_not_disable_another(self):
        # dots.mocr and YOLO run on separate endpoints, so a blank id
        # must turn off that engine alone.
        self.assertFalse(runpod_client.enabled(""))
        self.assertTrue(runpod_client.enabled("ep-2"))


class TestEndpointConfig(TestCase):
    """``endpoint_config`` builds the URL from the id it is given."""

    @override_settings(RUNPOD_API_KEY="key-1")
    def test_builds_url_and_header(self):
        base_url, headers = runpod_client.endpoint_config("ep-1")
        self.assertEqual(base_url, _BASE)
        self.assertEqual(headers, _HEADERS)

    @override_settings(RUNPOD_API_KEY="key-1")
    def test_blank_endpoint_raises(self):
        with self.assertRaises(runpod_client.RunpodError) as caught:
            runpod_client.endpoint_config("")
        self.assertEqual(caught.exception.error_code, "NOT_CONFIGURED")

    @override_settings(RUNPOD_API_KEY="")
    def test_missing_key_raises(self):
        with self.assertRaises(runpod_client.RunpodError):
            runpod_client.endpoint_config("ep-1")


class TestRedactUrls(TestCase):
    """Presigned URLs are capabilities and must not reach a log."""

    def test_masks_both_signed_fields_and_keeps_the_key(self):
        redacted = runpod_client._redact_urls(
            {
                "action": "parse",
                "pdf_url": "https://s3/in?X-Amz-Signature=abc",
                "result_url": "https://s3/out?X-Amz-Signature=def",
                "result_key": "processing/1/jobs/analyze/r1-s0-a1.json",
            }
        )
        self.assertEqual(redacted["pdf_url"], "***")
        self.assertEqual(redacted["result_url"], "***")
        # The key alone carries no capability, and it is what a reader
        # needs to find the output later.
        self.assertEqual(
            redacted["result_key"],
            "processing/1/jobs/analyze/r1-s0-a1.json",
        )
        self.assertEqual(redacted["action"], "parse")

    def test_does_not_mutate_the_original(self):
        job_input = {"pdf_url": "https://s3/in?sig"}
        runpod_client._redact_urls(job_input)
        self.assertEqual(job_input["pdf_url"], "https://s3/in?sig")

    def test_no_signed_fields_is_a_plain_copy(self):
        job_input = {"action": "parse"}
        self.assertEqual(
            runpod_client._redact_urls(job_input), {"action": "parse"}
        )


# ── Submit ──────────────────────────────────────────────────────────
class TestSubmitJob(TestCase):
    """``submit_job`` makes one request and classifies the answer."""

    def _submit(self, response=None, exc=None):
        with patch("scanning.runpod_client.requests.post") as post:
            if exc is not None:
                post.side_effect = exc
            else:
                post.return_value = response
            return runpod_client.submit_job(
                _BASE, _HEADERS, {"action": "parse"}, label="dots.mocr"
            )

    def test_returns_the_job_id(self):
        self.assertEqual(
            self._submit(_mock_response(200, {"id": "job-1"})), "job-1"
        )

    def test_makes_exactly_one_request(self):
        # No in-call retry: a blip is the next tick's problem, and a
        # sleep here would stall the serial daemon loop.
        with patch("scanning.runpod_client.requests.post") as post:
            post.side_effect = requests.ConnectionError("boom")
            with self.assertRaises(runpod_client.RunpodTransientError):
                runpod_client.submit_job(_BASE, _HEADERS, {"action": "parse"})
        self.assertEqual(post.call_count, 1)

    def test_no_response_is_unanswered_not_a_refusal(self):
        # RunPod may have accepted the job and lost the answer, so the
        # work may be running and its PUT may still land.
        with self.assertRaises(runpod_client.RunpodTransientError) as caught:
            self._submit(exc=requests.ReadTimeout("no answer"))
        self.assertEqual(
            caught.exception.error_code, runpod_client.UNANSWERED_ERROR_CODE
        )

    def test_409_is_endpoint_busy(self):
        with self.assertRaises(runpod_client.RunpodEndpointBusy) as caught:
            self._submit(
                _mock_response(409, {"code": "ENDPOINT_PAUSED", "detail": "0"})
            )
        self.assertEqual(caught.exception.error_code, "ENDPOINT_PAUSED")

    def test_endpoint_paused_code_without_409_is_also_busy(self):
        with self.assertRaises(runpod_client.RunpodEndpointBusy):
            self._submit(_mock_response(400, {"code": "ENDPOINT_PAUSED"}))

    def test_endpoint_busy_is_a_transient_error(self):
        # So a caller that only knows the transient class still retries,
        # while one that checks for busy can spare the attempt.
        self.assertTrue(
            issubclass(
                runpod_client.RunpodEndpointBusy,
                runpod_client.RunpodTransientError,
            )
        )

    def test_5xx_is_transient_and_answered(self):
        # Refused with an answer: nothing accepted, nothing will upload.
        with self.assertRaises(runpod_client.RunpodTransientError) as caught:
            self._submit(_mock_response(503, {"detail": "unavailable"}))
        self.assertNotEqual(
            caught.exception.error_code, runpod_client.UNANSWERED_ERROR_CODE
        )

    def test_4xx_is_terminal(self):
        with self.assertRaises(runpod_client.RunpodError) as caught:
            self._submit(_mock_response(401, {"detail": "bad key"}))
        self.assertNotIsInstance(
            caught.exception, runpod_client.RunpodTransientError
        )

    def test_no_job_id_is_terminal(self):
        with self.assertRaises(runpod_client.RunpodError) as caught:
            self._submit(_mock_response(200, {"status": "ok"}))
        self.assertEqual(caught.exception.error_code, "BAD_RESPONSE")

    def test_non_json_body_is_terminal(self):
        with self.assertRaises(runpod_client.RunpodError) as caught:
            self._submit(_mock_response(200, None))
        self.assertEqual(caught.exception.error_code, "BAD_RESPONSE")

    def test_signed_urls_are_not_logged(self):
        with (
            patch("scanning.runpod_client.requests.post") as post,
            self.assertLogs("scanning.runpod_client", "INFO") as logs,
        ):
            post.return_value = _mock_response(200, {"id": "job-1"})
            runpod_client.submit_job(
                _BASE,
                _HEADERS,
                {"action": "parse", "pdf_url": "https://s3/in?X-Amz-Sig=abc"},
            )
        self.assertNotIn("X-Amz-Sig", "".join(logs.output))


# ── Poll ────────────────────────────────────────────────────────────
class TestPollOnce(TestCase):
    """``poll_once`` never sleeps and never raises."""

    def _poll(self, response=None, exc=None, result_key=""):
        with patch("scanning.runpod_client.requests.get") as get:
            if exc is not None:
                get.side_effect = exc
            else:
                get.return_value = response
            return runpod_client.poll_once(
                _BASE, _HEADERS, "job-1", "dots.mocr", result_key=result_key
            )

    def test_completed_carries_the_output(self):
        outcome = self._poll(
            _mock_response(
                200,
                {"status": "COMPLETED", "output": {"result_key": "k", "b": 1}},
            )
        )
        self.assertEqual(outcome.status, JobStatus.COMPLETED)
        self.assertEqual(outcome.output, {"result_key": "k", "b": 1})

    def test_in_queue_and_in_progress_are_distinguished(self):
        # The crossing into IN_PROGRESS is what turns a queue ceiling
        # into an execution budget, so the two must not collapse.
        queued = self._poll(_mock_response(200, {"status": "IN_QUEUE"}))
        self.assertEqual(queued.status, JobStatus.IN_QUEUE)
        running = self._poll(_mock_response(200, {"status": "IN_PROGRESS"}))
        self.assertEqual(running.status, JobStatus.IN_PROGRESS)

    def test_an_unknown_status_reads_as_still_at_work(self):
        # RunPod adding a state must not fail every job merely in it.
        outcome = self._poll(_mock_response(200, {"status": "WARMING_UP"}))
        self.assertEqual(outcome.status, JobStatus.IN_PROGRESS)
        self.assertEqual(outcome.provider_status, "WARMING_UP")

    def test_a_5xx_teaches_us_nothing(self):
        outcome = self._poll(_mock_response(502, {"detail": "gateway"}))
        self.assertIsNone(outcome.status)

    def test_a_network_blip_teaches_us_nothing(self):
        outcome = self._poll(exc=requests.ConnectionError("reset"))
        self.assertIsNone(outcome.status)

    def test_an_unparseable_body_teaches_us_nothing(self):
        outcome = self._poll(_mock_response(200, None))
        self.assertIsNone(outcome.status)

    def test_a_non_object_body_teaches_us_nothing(self):
        outcome = self._poll(_mock_response(200, ["not", "an", "object"]))
        self.assertIsNone(outcome.status)

    def test_completed_with_a_non_object_output_fails(self):
        outcome = self._poll(
            _mock_response(200, {"status": "COMPLETED", "output": "text"})
        )
        self.assertEqual(outcome.status, JobStatus.FAILED)
        self.assertEqual(outcome.error_code, "BAD_OUTPUT")
        self.assertFalse(outcome.retriable)

    def test_transient_worker_codes_are_retriable(self):
        for code in runpod_client.TRANSIENT_ERROR_CODES:
            with self.subTest(code=code):
                outcome = self._poll(
                    _mock_response(
                        200,
                        {
                            "status": "FAILED",
                            "output": {"error_code": code},
                            "error": "worker said so",
                        },
                    )
                )
                self.assertEqual(outcome.status, JobStatus.FAILED)
                self.assertEqual(outcome.error_code, code)
                self.assertTrue(outcome.retriable)

    def test_terminal_worker_codes_are_not_retriable(self):
        for code in ("BAD_INPUT", "UNKNOWN_ACTION", "RESULT_UPLOAD_REJECTED"):
            with self.subTest(code=code):
                outcome = self._poll(
                    _mock_response(
                        200,
                        {
                            "status": "FAILED",
                            "output": {"error_code": code},
                        },
                    )
                )
                self.assertFalse(outcome.retriable)

    def test_failed_with_no_output_is_a_platform_fault(self):
        # No handler code means the failure happened outside the
        # handler: a worker crash, an internal timeout. Retry it.
        outcome = self._poll(
            _mock_response(200, {"status": "FAILED", "error": "job timed out"})
        )
        self.assertEqual(outcome.status, JobStatus.FAILED)
        self.assertTrue(outcome.retriable)

    def test_timed_out_and_cancelled_are_not_retriable(self):
        for provider_status in ("TIMED_OUT", "CANCELLED"):
            with self.subTest(provider_status=provider_status):
                outcome = self._poll(
                    _mock_response(200, {"status": provider_status})
                )
                self.assertEqual(outcome.status, JobStatus.FAILED)
                self.assertEqual(outcome.error_code, provider_status)
                self.assertFalse(outcome.retriable)


class TestPollOnceMissingJob(TestCase):
    """A 404 asks S3 before it writes a paid job off."""

    def _poll_404(self, result_key="jobs/analyze/r1-s0-a1.json", **kwargs):
        with (
            patch("scanning.runpod_client.requests.get") as get,
            patch("scanning.s3_sync.object_exists", **kwargs) as exists,
        ):
            get.return_value = _mock_response(404, {"detail": "not found"})
            outcome = runpod_client.poll_once(
                _BASE, _HEADERS, "job-1", result_key=result_key
            )
        return outcome, exists

    def test_output_on_s3_completes_the_job(self):
        outcome, exists = self._poll_404(return_value=True)
        self.assertEqual(outcome.status, JobStatus.COMPLETED)
        self.assertEqual(
            outcome.output, {"result_key": "jobs/analyze/r1-s0-a1.json"}
        )
        exists.assert_called_once_with("jobs/analyze/r1-s0-a1.json")

    def test_no_output_expires_the_job_but_stays_retriable(self):
        # The inputs are still on S3; only the job record is gone.
        outcome, _ = self._poll_404(return_value=False)
        self.assertEqual(outcome.status, JobStatus.EXPIRED)
        self.assertEqual(outcome.error_code, "JOB_NOT_FOUND")
        self.assertTrue(outcome.retriable)

    def test_an_s3_failure_is_swallowed_not_raised(self):
        # Raising would abort the sweep for every other job on the tick.
        outcome, _ = self._poll_404(side_effect=RuntimeError("s3 down"))
        self.assertEqual(outcome.status, JobStatus.EXPIRED)
        self.assertTrue(outcome.retriable)

    def test_no_result_key_skips_the_probe(self):
        outcome, exists = self._poll_404(result_key="", return_value=True)
        self.assertEqual(outcome.status, JobStatus.EXPIRED)
        exists.assert_not_called()


# ── Cancel ──────────────────────────────────────────────────────────
class TestCancelJob(TestCase):
    """Cancelling stops the billing, and its own failure is not fatal."""

    def test_posts_to_cancel(self):
        with patch("scanning.runpod_client.requests.post") as post:
            runpod_client.cancel_job(_BASE, _HEADERS, "job-1")
        post.assert_called_once()
        self.assertEqual(post.call_args[0][0], f"{_BASE}/cancel/job-1")

    def test_a_blank_id_is_a_no_op(self):
        with patch("scanning.runpod_client.requests.post") as post:
            runpod_client.cancel_job(_BASE, _HEADERS, "")
        post.assert_not_called()

    def test_a_failure_never_raises(self):
        with patch("scanning.runpod_client.requests.post") as post:
            post.side_effect = requests.ConnectionError("reset")
            runpod_client.cancel_job(_BASE, _HEADERS, "job-1")
