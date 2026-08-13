"""The compute providers an :class:`~scanning.models.ExternalJob` runs on.

The daemon holds many jobs at once across several providers and must
not learn what any of them is. Everything it does to a job goes
through :class:`ComputeProvider`: submit it, ask after it, read what it
produced, cancel it. Dispatch is on ``job.provider``, so adding doctor
(#158) or a hosted OCR API is a new subclass and a registry entry, not
a change to the scheduling loop.

The vocabulary the loop speaks in -- :class:`SubmitReceipt` and
:class:`PollOutcome` -- lives here rather than in any one client, since
a provider's job is precisely to answer in it. RunPod hands back a job
id and a ``/status`` endpoint; Mistral and doctor answer on their own
endpoints in their own shapes. Only the subclass sees the difference.

Provider modules are imported inside the methods rather than at the top
of this one. It keeps a client's import cost off every caller that only
wanted the registry, and it is the idiom ``services.py`` already uses.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from scanning.models import ExternalJob


@dataclass(frozen=True)
class SubmitReceipt:
    """What a submitted job leaves behind so a later poll can find it.

    Every field maps onto an ``ExternalJob`` column, because the process
    that polls a job is generally not the one that submitted it. Nothing
    about an in-flight job may live only in a call stack.

    :ivar external_id: The provider's own job id.
    :ivar result_key: S3 key the worker was authorized to write to, or
        ``""`` when the result comes back inline.
    :ivar submitted_at: Submission time (UTC). Compared against the
        result object's ``LastModified`` to tell this attempt's output
        from an earlier one's.
    """

    external_id: str
    result_key: str
    submitted_at: datetime


@dataclass(frozen=True)
class PollOutcome:
    """One status answer, normalized onto :class:`JobStatus`.

    A value rather than an exception because the collect phase sweeps
    every in-flight job on one tick, and one job's terminal failure
    must not abort the sweep for the rest.

    :ivar status: A ``JobStatus`` value, or ``None`` when the status
        call itself failed and we learned nothing. ``None`` is not a
        job state: it means ask again, and the job stays as it was.
    :ivar provider_status: The provider's own status string, kept for
        logs and progress messages. Empty when there was no answer.
    :ivar output: The provider's result envelope on ``COMPLETED``.
    :ivar error_code: The worker's ``error_code``, or one the client
        synthesised for a failure the provider reported no code for.
    :ivar error_message: Human-readable failure detail.
    :ivar retriable: Whether resubmitting could plausibly succeed.
        Orthogonal to whether ``status`` is terminal: ``EXPIRED`` is
        both terminal and worth another submit, since the inputs are
        still on S3 and only the job record is gone.
    """

    status: str | None
    provider_status: str = ""
    output: dict | None = None
    error_code: str = ""
    error_message: str = ""
    retriable: bool = False


class ComputeProvider(ABC):
    """One place that runs jobs, and the four things we do to them.

    Implementations are stateless and shared; a job row carries
    everything a call needs.
    """

    #: The :class:`~scanning.models.JobProvider` value this serves.
    name: str

    @abstractmethod
    def submit(
        self, job: ExternalJob, payload: dict[str, Any]
    ) -> SubmitReceipt:
        """Hand ``job`` over and return without waiting for it.

        :param job: The row to submit. Its ``result_key`` names the
            object the worker may write, so two live attempts of one
            shard cannot presign the same one.
        :param payload: Engine-specific input fields. The provider adds
            whatever its own transport needs.
        :returns: What the caller must persist to poll this job later.
        :rtype: SubmitReceipt
        """

    @abstractmethod
    def poll(self, job: ExternalJob) -> PollOutcome:
        """Ask after one job once, without sleeping.

        Pacing belongs to the daemon's tick. A backoff inside here
        would serialize the batch it exists to let run at once.

        :param job: The in-flight row to ask about.
        :returns: What this poll learned.
        :rtype: PollOutcome
        """

    @abstractmethod
    def fetch_result(self, job: ExternalJob, output: dict) -> dict:
        """Resolve a completed job's output into the payload we apply.

        :param job: The completed row.
        :param output: The ``output`` from the job's :class:`PollOutcome`.
        :returns: The payload, guaranteed to describe this job.
        :rtype: dict
        """

    @abstractmethod
    def cancel(self, job: ExternalJob) -> None:
        """Stop paying for a job we no longer want.

        Best effort: a cancel that fails must not stall the sweep, so
        implementations log rather than raise.

        :param job: The row to cancel.
        """


class RunPodProvider(ComputeProvider):
    """RunPod Serverless, over ``scanning.runpod_client``.

    ``job.stage`` doubles as the handler's ``action``: the stages
    RunPod serves (``detect``, ``analyze``) are named for the actions
    that serve them. A stage RunPod does not implement never reaches
    here, because the pipeline picks the provider per stage.
    """

    name = "runpod"

    def submit(self, job, payload):
        """Submit via ``POST /run``. See :meth:`ComputeProvider.submit`."""
        from scanning import runpod_client

        return runpod_client.submit_job(
            action=job.stage,
            scan=job.scan,
            payload=payload,
            result_key=job.result_key or None,
        )

    def poll(self, job):
        """Ask ``GET /status``. See :meth:`ComputeProvider.poll`."""
        from scanning import runpod_client

        base_url, headers = runpod_client.endpoint_config()
        return runpod_client.poll_once(
            base_url,
            headers,
            job.external_id,
            job.stage,
            result_key=job.result_key or None,
            submitted_at=job.submitted_at,
        )

    def fetch_result(self, job, output):
        """Read the S3 object. See :meth:`ComputeProvider.fetch_result`."""
        from scanning import runpod_client

        return runpod_client.harvest(
            output,
            job.scan,
            job.stage,
            job.result_key or None,
            job.submitted_at,
            job.external_id,
        )

    def cancel(self, job):
        """POST ``/cancel``. See :meth:`ComputeProvider.cancel`."""
        from scanning import runpod_client

        base_url, headers = runpod_client.endpoint_config()
        runpod_client.cancel_job(base_url, headers, job.external_id)


#: Built once and shared: implementations hold no per-job state.
_PROVIDERS: dict[str, ComputeProvider] = {
    p.name: p for p in (RunPodProvider(),)
}


def get_provider(name: str) -> ComputeProvider:
    """Return the implementation serving ``name``.

    :param name: A :class:`~scanning.models.JobProvider` value.
    :returns: The provider.
    :rtype: ComputeProvider
    :raises NotImplementedError: For a provider the model offers but
        nothing implements yet (doctor, Mistral). Naming it in the
        enum is how the schema is kept stable ahead of the code; this
        is where that gap surfaces rather than as an ``AttributeError``
        three frames deeper.
    """
    try:
        return _PROVIDERS[name]
    except KeyError:
        raise NotImplementedError(
            f"no compute provider implements {name!r}; "
            f"implemented: {', '.join(sorted(_PROVIDERS))}"
        ) from None
