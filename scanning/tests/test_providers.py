"""Tests for ``scanning.providers``.

The registry and the RunPod adapter. RunPod's own wire behaviour is
covered in ``test_runpod_client``; what matters here is that the
adapter reads the job row rather than its arguments, since the process
polling a job is generally not the one that submitted it.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from scanning import providers
from scanning.factories import ExternalJobFactory
from scanning.models import JobProvider, JobStatus

_SUBMITTED_AT = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


class TestRegistry(TestCase):
    """``get_provider`` resolves a job's provider, or says why it can't."""

    def test_runpod_resolves(self):
        provider = providers.get_provider(JobProvider.RUNPOD)
        self.assertIsInstance(provider, providers.RunPodProvider)
        self.assertEqual(provider.name, JobProvider.RUNPOD)

    def test_providers_are_shared_not_rebuilt(self):
        # Implementations hold no per-job state, so the loop should not
        # pay to construct one per job it sweeps.
        self.assertIs(
            providers.get_provider(JobProvider.RUNPOD),
            providers.get_provider(JobProvider.RUNPOD),
        )

    def test_every_declared_provider_answers_clearly(self):
        # The enum names providers ahead of their code (#158). An
        # unimplemented one must say so here, not fail three frames
        # deeper with an AttributeError on None.
        for value in JobProvider.values:
            with self.subTest(provider=value):
                try:
                    providers.get_provider(value)
                except NotImplementedError as exc:
                    self.assertIn(value, str(exc))
                    self.assertIn("runpod", str(exc))

    def test_unknown_provider_raises(self):
        with self.assertRaises(NotImplementedError):
            providers.get_provider("nonesuch")


class TestComputeProviderContract(TestCase):
    """The ABC refuses a subclass that leaves an operation unwritten."""

    def test_partial_implementation_cannot_be_instantiated(self):
        class Halfway(providers.ComputeProvider):
            name = "halfway"

            def submit(self, job, payload):
                return None

        with self.assertRaises(TypeError):
            Halfway()


@override_settings(
    RUNPOD_ENABLED=True,
    RUNPOD_ENDPOINT_ID="ep-abc",
    RUNPOD_API_KEY="apikey",
)
class TestRunPodProvider(TestCase):
    """The adapter takes its arguments from the job row."""

    def setUp(self):
        self.provider = providers.RunPodProvider()
        self.job = ExternalJobFactory(
            external_id="job-xyz",
            result_key="processing/1/a/1/1/jobs/detect/r1-s0-a1.json",
            submitted_at=_SUBMITTED_AT,
            status=JobStatus.IN_PROGRESS,
        )

    def test_submit_presigns_the_rows_own_key(self):
        # Not the per-scan default: several jobs of one scan are live at
        # once, and the default would have them all write to one object.
        with patch("scanning.runpod_client.submit_job") as mock_submit:
            self.provider.submit(self.job, {"pdf_url": "https://x/y.pdf"})

        _, kwargs = mock_submit.call_args
        self.assertEqual(kwargs["result_key"], self.job.result_key)
        self.assertEqual(kwargs["action"], self.job.stage)
        self.assertEqual(kwargs["scan"], self.job.scan)

    def test_submit_without_a_key_falls_back_to_the_default(self):
        self.job.result_key = ""
        with patch("scanning.runpod_client.submit_job") as mock_submit:
            self.provider.submit(self.job, {})
        self.assertIsNone(mock_submit.call_args.kwargs["result_key"])

    def test_poll_asks_after_the_rows_job(self):
        with patch("scanning.runpod_client.poll_once") as mock_poll:
            self.provider.poll(self.job)

        args, kwargs = mock_poll.call_args
        self.assertEqual(args[2], "job-xyz")
        self.assertEqual(args[3], self.job.stage)
        self.assertEqual(kwargs["result_key"], self.job.result_key)
        self.assertEqual(kwargs["submitted_at"], _SUBMITTED_AT)

    def test_poll_returns_the_neutral_outcome(self):
        outcome = providers.PollOutcome(
            status=JobStatus.IN_QUEUE, provider_status="IN_QUEUE"
        )
        with patch("scanning.runpod_client.poll_once", return_value=outcome):
            self.assertIs(self.provider.poll(self.job), outcome)

    def test_fetch_result_harvests_against_the_rows_provenance(self):
        with patch("scanning.runpod_client.harvest") as mock_harvest:
            self.provider.fetch_result(self.job, {"result_key": "k"})

        args, _ = mock_harvest.call_args
        self.assertEqual(args[0], {"result_key": "k"})
        self.assertEqual(args[1], self.job.scan)
        self.assertEqual(args[2], self.job.stage)
        self.assertEqual(args[3], self.job.result_key)
        self.assertEqual(args[4], _SUBMITTED_AT)
        self.assertEqual(args[5], "job-xyz")

    def test_cancel_hits_the_endpoint(self):
        with patch(
            "scanning.runpod_client.requests.post",
            return_value=MagicMock(status_code=200),
        ) as mock_post:
            self.provider.cancel(self.job)

        self.assertIn("/cancel/job-xyz", mock_post.call_args.args[0])

    def test_cancel_swallows_a_failure(self):
        # Best effort: a cancel that fails must not abort the sweep of
        # every other job on the tick.
        with patch(
            "scanning.runpod_client.requests.post",
            side_effect=RuntimeError("network down"),
        ):
            self.provider.cancel(self.job)
