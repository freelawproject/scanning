"""Models for the LLM-driven extraction pipeline.

Scope boundary: ``scanning`` owns artifacts produced by the file-
generation pipeline (PDFs, per-page rows, opinion splits). ``ai`` owns
the *processing* side that turns those artifacts into XML — prompts,
batch requests, and per-task records.

Vendored from CourtListener's ``cl/ai/models.py`` (via the scanning-
testing-project's ``llm_ai/models.py``). The vendor boundary is kept
so this can graduate back upstream cleanly. The only local-only
substitution is ``AbstractDateTimeModel`` (imported from
``scanning.models`` instead of ``cl.lib.models``); upstream would
swap that import on the way back in.
"""

from __future__ import annotations

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType  # noqa: F401
from django.db import models

from scanning.models import AbstractDateTimeModel


# ── enums ─────────────────────────────────────────────────────────────


class PromptTypes(models.IntegerChoices):
    SYSTEM = 1, "System"
    USER = 2, "User"


class LLMProvider(models.IntegerChoices):
    GEMINI = 1, "Google Gemini"
    OPENAI = 2, "OpenAI"
    ANTHROPIC = 3, "Anthropic"



class LLMTaskStatusChoices(models.IntegerChoices):
    UNPROCESSED = 0, "Unprocessed"
    IN_PROGRESS = 1, "In Progress"
    SUCCEEDED = 2, "LLM response received"
    FAILED = 3, "Failed"
    FINISHED = 4, "LLM response processed"


class LLMRequestStatusChoices(models.IntegerChoices):
    UNPROCESSED = 0, "Unprocessed"
    IN_PROGRESS = 1, "In Progress"
    SUCCEEDED = 2, "LLM response received"
    FAILED = 3, "Failed"
    FINISHED = 4, "LLM response processed"


# ── models ────────────────────────────────────────────────────────────


class Prompt(AbstractDateTimeModel):
    """A system or user prompt text used by the LLM extraction pipeline.

    Rows are immutable in normal usage: a tweaked prompt is a new row,
    not an edit. ``scanning.Page.user_prompt`` repoints to the new row,
    leaving the prior Prompt as queryable history.
    """

    name = models.CharField(max_length=255, blank=True)
    prompt_type = models.SmallIntegerField(
        choices=PromptTypes.choices,
        default=PromptTypes.SYSTEM,
    )
    text = models.TextField()
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Prompt"
        verbose_name_plural = "Prompts"

    def __str__(self) -> str:
        label = self.name or f"Prompt {self.pk}"
        return f"{label} ({self.get_prompt_type_display()})"


class LLMBatch(AbstractDateTimeModel):
    """One batch (or single) submission to an LLM provider.

    Wraps ``LLMTask`` rows: a batch of N pages becomes 1 LLMRequest +
    N LLMTasks. ``batch_id`` is the provider's identifier; ``status``
    is the overall batch state. Cost totals roll up from the tasks.
    """

    name = models.CharField(max_length=255, blank=True)
    is_batch = models.BooleanField(default=False)
    batch_id = models.CharField(max_length=255, blank=True)
    provider = models.SmallIntegerField(
        choices=LLMProvider.choices,
        default=LLMProvider.GEMINI,
    )
    api_model_name = models.CharField(max_length=100, blank=True)
    status = models.SmallIntegerField(
        choices=LLMRequestStatusChoices.choices,
        default=LLMRequestStatusChoices.UNPROCESSED,
    )
    prompts = models.ManyToManyField(
        Prompt,
        related_name="requests",
        blank=True,
    )
    total_tasks = models.IntegerField(default=0)
    completed_tasks = models.IntegerField(default=0)
    failed_tasks = models.IntegerField(default=0)
    max_retries = models.SmallIntegerField(default=3)
    total_cost_estimate = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        null=True,
        blank=True,
    )
    total_cost_actual = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        null=True,
        blank=True,
    )
    date_started = models.DateTimeField(null=True, blank=True)
    date_completed = models.DateTimeField(null=True, blank=True)
    extra_config_params = models.JSONField(default=dict, blank=True)
    # Upstream uses S3PrivateLLMStorage; locally we keep on disk and
    # let the deployment swap the storage class in via settings.
    batch_response_file = models.FileField(
        upload_to="llm_requests/",
        max_length=1000,
        blank=True,
    )

    class Meta:
        verbose_name = "LLM Request"
        verbose_name_plural = "LLM Requests"

    def __str__(self) -> str:
        label = self.name or self.batch_id or f"Request {self.pk}"
        return f"{label} ({self.get_status_display()})"


class LLMTask(AbstractDateTimeModel):
    """One work item submitted to an LLM — typically a per-page
    extraction attempt against a ``scanning.Page``.

    Generic relation (``content_object``) so the same plumbing can be
    reused for non-page LLM work (case-name extraction on a Docket,
    citation cleanup on an Opinion, etc.). Each retry is a new row;
    history is preserved.
    """

    status = models.SmallIntegerField(
        choices=LLMTaskStatusChoices.choices,
        default=LLMTaskStatusChoices.UNPROCESSED,
    )
    llm_key = models.CharField(max_length=255)
    retry_count = models.SmallIntegerField(default=0)
    error_message = models.TextField(blank=True)

    input_file = models.FileField(
        upload_to="llm_tasks/inputs/",
        max_length=1000,
        blank=True,
    )
    input_text = models.TextField(blank=True)
    response_file = models.FileField(
        upload_to="llm_tasks/responses/",
        max_length=1000,
        blank=True,
    )

    processing_time_ms = models.IntegerField(null=True, blank=True)
    date_started = models.DateTimeField(null=True, blank=True)
    date_completed = models.DateTimeField(null=True, blank=True)

    content_type = models.ForeignKey(
        ContentType,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        db_index=False,
    )
    object_id = models.PositiveIntegerField(null=True, blank=True)
    content_object = GenericForeignKey("content_type", "object_id")

    request = models.ForeignKey(
        LLMBatch,
        related_name="tasks",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = "LLM Task"
        verbose_name_plural = "LLM Tasks"
        indexes = [models.Index(fields=["content_type", "object_id"])]

    def __str__(self) -> str:
        return f"Task {self.pk}: {self.get_status_display()}"
