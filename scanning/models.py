import logging
import uuid
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.deconstruct import deconstructible

from scanning.storage import LocalProcessingStorage

logger = logging.getLogger(__name__)

_local_storage = LocalProcessingStorage()

# Legal ``PageEdit.value`` entries for a ROTATE_PAGE row: clockwise
# degrees. A module constant because a check constraint is declared
# inside ``Meta``, which cannot read the enclosing class body.
PAGE_EDIT_ROTATIONS = ("90", "180", "270")


class AutoNowQuerySet(models.QuerySet):
    """QuerySet that stamps ``auto_now`` fields on bulk writes.

    Django's ``QuerySet.update()`` and ``QuerySet.bulk_update()`` both
    bypass the ``pre_save`` hooks that normally maintain ``auto_now``
    fields, so ``date_modified`` (and any other ``auto_now`` field)
    never advances when rows are written via either path. This
    QuerySet introspects the model and stamps every ``auto_now`` field
    to ``timezone.now()``, unless the caller explicitly provided a
    value.

    :cvar model: The model class this QuerySet is bound to.
    """

    def update(self, **kwargs):
        """Update rows, stamping ``auto_now`` fields with the current time.

        :param kwargs: Fields and values to update. Any ``auto_now`` field
            not already in ``kwargs`` is set to ``timezone.now()``.
        :returns: The number of rows matched by the update.
        :rtype: int
        """
        for field in self.model._meta.get_fields():
            if getattr(field, "auto_now", False):
                kwargs.setdefault(field.name, timezone.now())
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        """Bulk-update rows, stamping ``auto_now`` fields on each instance.

        Fields already listed in ``fields`` are respected as-is (the
        caller is setting them explicitly). Any ``auto_now`` field not
        in ``fields`` is appended and stamped with ``timezone.now()``
        on every instance.

        :param objs: Iterable of model instances to update.
        :param fields: Iterable of field names to write.
        :param batch_size: Optional batch size, forwarded to Django.
        :returns: The number of rows matched by the update.
        :rtype: int
        """
        fields = list(fields)
        auto_now_fields = [
            f.name
            for f in self.model._meta.get_fields()
            if getattr(f, "auto_now", False) and f.name not in fields
        ]
        if auto_now_fields:
            now = timezone.now()
            objs = list(objs)
            fields.extend(auto_now_fields)
            for obj in objs:
                for name in auto_now_fields:
                    setattr(obj, name, now)
        return super().bulk_update(objs, fields, batch_size=batch_size)


class Status(models.TextChoices):
    """Where a scan is in the pipeline.

    Three of these mean "busy" (see :data:`BUSY_STATUSES`), and they
    differ in who is to blame when a scan stops moving:

    - ``QUEUED``: waiting for the daemon to claim it.
    - ``PROCESSING``: a daemon thread is in the pipeline right now.
      Both the stale-row sweep and the SIGTERM handler re-queue these
      and charge an interruption, assuming no daemon is on them.
    - ``AWAITING``: nothing of ours is running; external jobs are. A
      scan may sit here as long as its jobs' own deadlines allow, so it
      must *not* be swept -- that would charge an interruption for
      waiting and redo work already paid for.
    """

    UPLOADED = "uploaded", "Uploaded"
    QUEUED = "queued", "Queued"
    PROCESSING = "processing", "Processing"
    # Parked out of PROCESSING while external jobs run (issue #176):
    # the shards are with doctor, and later RunPod. Progress lives on
    # the ExternalJob rows rather than in a call stack, which is what
    # lets a killed daemon resume by reading them.
    AWAITING = "awaiting", "Waiting on external jobs"
    # Parking state (issues #173/#154): the upload-side pipeline
    # finished, but one or more inputs of the page completeness review
    # are still outstanding -- the bitonal preview, the dots.mocr run,
    # or the page numbers and issues from #149. Scans wait here, out
    # of the review flow, so nothing downstream mistakes them for
    # reviewed or errored volumes.
    AWAITING_VALIDATION = "awaiting_validation", "Awaiting Validation"
    # The two page-completeness review states (#154). Both are parked
    # human states, not busy ones: the viewer does not poll them and
    # the stale sweep must not touch them. The #149 apply and its
    # recomputations write READY_FOR_PAGE_COMPLETENESS_REVIEW, and the
    # approve button (#151,
    # views_process.approve_page_completeness) is the only writer of
    # PAGE_COMPLETENESS_REVIEW_DONE. The stages behind review 1
    # (#195/#196) trigger off DONE and write no scan status, so
    # redaction work never blocks either review.
    READY_FOR_PAGE_COMPLETENESS_REVIEW = (
        "ready_for_page_completeness_review",
        "Ready for page review",
    )
    PAGE_COMPLETENESS_REVIEW_DONE = (
        "page_completeness_review_done",
        "Page review done",
    )
    # The two redaction review states (#263), the same shape as the
    # two above. ``review_states.redaction_review_ready`` is the whole
    # rule behind READY -- review 1 approved, the page complete volume
    # built (#224), and the redactions computed from the detection run
    # -- and it has two callers: the redaction apply parks in READY
    # (``services._park_after_redactions``), and
    # ``review_states.promote_ready_scans`` catches on the collect tick
    # what the apply could not see yet. ``approve_redaction_review``
    # (#263, views_process) is the only writer of
    # REDACTION_REVIEW_DONE, and that approval is the gate of step 3.
    # Parked human states again: no polling, no sweep.
    READY_FOR_REDACTION_REVIEW = (
        "ready_for_redaction_review",
        "Ready for redaction review",
    )
    REDACTION_REVIEW_DONE = (
        "redaction_review_done",
        "Redaction review done",
    )
    PENDING_REVIEW = "pending_review", "Pending Review"
    APPROVED = "approved", "Approved"
    EXTRACTED = "extracted", "Extracted"
    ERROR = "error", "Error"
    ERROR_MAX_RETRIES = "error_max_retries", "Error (retry cap hit)"
    ERROR_INTERRUPTED = "error_interrupted", "Error (interrupted too often)"
    # Legacy (#219): the user cancel that wrote this was unreachable --
    # no template ever rendered its button -- and is deleted. The value
    # stays because historical rows hold it, and because the tests use
    # it as a status the pipeline must not stomp. Nothing writes it now.
    # A future cancel should abandon the job rows and leave the status
    # to the daemon (#212), not revive this.
    CANCELLED = "cancelled", "Cancelled"


#: Statuses meaning "work on this scan is under way" -- queued,
#: running, or waiting on a provider. The viewer polls for these.
#: Deliberately not a substitute for the narrower ``status=PROCESSING``
#: guards: only PROCESSING may be swept as stale.
BUSY_STATUSES = frozenset({Status.QUEUED, Status.PROCESSING, Status.AWAITING})

#: The parked human states of the two reviews (#154, #263). None of
#: them is busy: nothing polls them and the stale sweep never touches
#: them. A recompute that rebuilds data underneath a review must
#: preserve whichever one the scan holds, which is what this set is
#: read for (``services.recalculate_issues``). The legacy
#: ``PENDING_REVIEW`` is not here: it is the status such a recompute
#: writes for the rows that never entered this flow.
REVIEW_STATUSES = frozenset(
    {
        Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
        Status.PAGE_COMPLETENESS_REVIEW_DONE,
        Status.READY_FOR_REDACTION_REVIEW,
        Status.REDACTION_REVIEW_DONE,
    }
)

#: The statuses that say "a person approved the page completeness".
#: `PAGE_COMPLETENESS_REVIEW_DONE` is where that approval lands, and
#: the two #263 states are further along the same road, so the approval
#: holds in all three. Read by the step-1 bar (`_review_flags`), whose
#: mark and whose "Next: Detect" button describe review 1 alone: a
#: curator who walks back to step 1 from review 2 must see the same
#: bar they left, and `start_detect` accepts all three.
PAGE_REVIEW_APPROVED_STATUSES = frozenset(
    {
        Status.PAGE_COMPLETENESS_REVIEW_DONE,
        Status.READY_FOR_REDACTION_REVIEW,
        Status.REDACTION_REVIEW_DONE,
    }
)


class Stage(models.TextChoices):
    VALIDATE = "validate", "Validate"
    PROCESS = "process", "Process"
    APPROVED = "approved", "Approved"


class QueuedAction(models.TextChoices):
    FULL_PIPELINE = "full_pipeline", "Full Pipeline"
    VALIDATE = "validate", "Validate"
    DETECT = "detect", "Detect"
    REPROCESS = "reprocess", "Reprocess"
    GENERATE_FILES = "generate_files", "Generate Files"
    # Issue #196: turn a merged detection run into Detection rows,
    # paired opinions and redaction geometry. Queued work rather than a
    # pass on the collect tick, because it renders every page of the
    # volume three times (~83s for 1364 pages).
    COMPUTE_REDACTIONS = "compute_redactions", "Compute Redactions"
    # Issue #224: build the final volume from the original plus the
    # PageEdit rows, and glue the paid results into its space. Queued
    # work in two phases (build, glue), because both pull and write
    # whole volumes.
    APPLY_PAGE_EDITS = "apply_page_edits", "Apply Page Edits"


class UploadAction(models.TextChoices):
    """What to do with a scan once its original PDF is stored.

    Chosen by the uploader (which submit button) and applied by
    ``_finalize_uploaded_scan`` / the recovery command.
    """

    UPLOAD_ONLY = "upload_only", "Upload only"
    UPLOAD_VALIDATE = "upload_validate", "Upload and validate"


class Priority(models.TextChoices):
    CRITICAL = "critical", "Critical"
    HIGH = "high", "High"
    MEDIUM = "medium", "Medium"
    LOW = "low", "Low"
    BACKLOG = "backlog", "Backlog"


class QueueStatus(models.TextChoices):
    NEEDS_SCANNING = "needs_scanning", "Needs Scanning"
    ASSIGNED = "assigned", "Assigned"
    SCANNING = "scanning", "Scanning"
    SCANNED = "scanned", "Scanned"
    COMPLETE = "complete", "Complete"
    UNAVAILABLE = "unavailable", "Unavailable"


class Source(models.TextChoices):
    FULL = "full", "Full (Archival version)"
    OPINIONS = "opinions", "Opinions only version"


class OpinionStatus(models.TextChoices):
    """Review status for an individual opinion scan.

    ``GAP`` indicates missing pages within an opinion's page range,
    e.g. pages were skipped or lost during scanning.
    """

    NO_STATUS = "no_status", "No status"
    OK = "ok", "OK (opinion is correct)"
    GAP = "gap", "Gap (missing pages in opinion)"
    ERROR = "error", "Error"


class AbstractDateTimeModel(models.Model):
    """An abstract base class for most models."""

    date_created = models.DateTimeField(
        help_text="The moment when the item was created.",
        auto_now_add=True,
        db_index=True,
    )
    date_modified = models.DateTimeField(
        help_text="The last moment when the item was modified.",
        auto_now=True,
        db_index=True,
    )

    objects = AutoNowQuerySet.as_manager()

    def save(self, *args, update_fields=None, **kwargs):
        """Save, ensuring ``auto_now`` fields are included in ``update_fields``.

        Django's ``save(update_fields=[...])`` silently skips ``auto_now``
        fields unless they are listed explicitly, so ``date_modified`` is
        never advanced. This override adds every ``auto_now`` field on the
        model to ``update_fields`` so ``save(update_fields=["foo"])``
        always stamps ``date_modified`` as callers expect.

        :param args: Positional args forwarded to ``Model.save``.
        :param update_fields: Iterable of field names to write, or None
            to write all fields.
        :param kwargs: Keyword args forwarded to ``Model.save``.
        """
        if update_fields is not None:
            update_fields = set(update_fields)
            for field in self._meta.get_fields():
                if getattr(field, "auto_now", False):
                    update_fields.add(field.name)
        super().save(*args, update_fields=update_fields, **kwargs)

    class Meta:
        abstract = True


class Reporter(AbstractDateTimeModel):
    """A legal reporter series (e.g. U.S. Reports, Federal Reporter)."""

    # Mapping from short_name to Bluebook citation abbreviation.
    CITE_MAP = {
        "a": "A.",
        "a2d": "A.2d",
        "a3d": "A.3d",
        "br": "B.R.",
        "f": "F.",
        "f2d": "F.2d",
        "f3d": "F.3d",
        "f4th": "F.4th",
        "f-appx": "F. App'x",
        "f-supp": "F. Supp.",
        "f-supp-2d": "F. Supp. 2d",
        "f-supp-3d": "F. Supp. 3d",
        "ne": "N.E.",
        "ne2d": "N.E.2d",
        "ne3d": "N.E.3d",
        "nw": "N.W.",
        "nw2d": "N.W.2d",
        "p": "P.",
        "p2d": "P.2d",
        "p3d": "P.3d",
        "se": "S.E.",
        "se2d": "S.E.2d",
        "so": "So.",
        "so2d": "So. 2d",
        "so3d": "So. 3d",
        "sw": "S.W.",
        "sw2d": "S.W.2d",
        "sw3d": "S.W.3d",
        "s-ct": "S. Ct.",
        "us": "U.S.",
        "l-ed": "L. Ed.",
        "l-ed-2d": "L. Ed. 2d",
        "am-tribal-law": "Am. Tribal Law",
    }

    short_name = models.CharField(max_length=20, unique=True, db_index=True)
    full_name = models.CharField(max_length=100)

    class Meta:
        ordering = ["full_name"]

    @property
    def cite_name(self):
        """Bluebook citation abbreviation (e.g. 'a3d' → 'A.3d')."""
        return self.CITE_MAP.get(self.short_name, self.short_name.upper())

    def __str__(self):
        return self.full_name


class Volume(AbstractDateTimeModel):
    """A logical volume in the scanning queue.

    One volume may require multiple scans (e.g. a volume split into
    books A/B/C, or advance sheets covering different page ranges).
    """

    reporter = models.ForeignKey(
        Reporter,
        on_delete=models.PROTECT,
        related_name="volumes",
    )
    volume_number = models.PositiveIntegerField(
        validators=[MinValueValidator(1)]
    )
    expected_start_page = models.PositiveIntegerField(null=True, blank=True)
    expected_end_page = models.PositiveIntegerField(null=True, blank=True)
    priority = models.CharField(
        max_length=20,
        choices=Priority.choices,
        default=Priority.MEDIUM,
    )
    queue_status = models.CharField(
        max_length=20,
        choices=QueueStatus.choices,
        default=QueueStatus.NEEDS_SCANNING,
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_volumes",
    )
    assigned_at = models.DateTimeField(null=True, blank=True)
    source_library = models.CharField(max_length=200, blank=True, default="")
    source_url = models.URLField(blank=True, default="")
    is_partial = models.BooleanField(
        default=False,
        help_text=(
            "Volume is split into multiple parts"
            " (e.g. books A/B or advance sheets)."
        ),
    )
    expected_parts = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="How many scans make up this volume.",
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["reporter", "volume_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["reporter", "volume_number"],
                name="unique_volume_reporter_number",
            ),
        ]

    @property
    def scans_complete(self):
        return self.scans.filter(status=Status.APPROVED).count()

    @property
    def coverage(self):
        ranges = []
        for s in self.scans.order_by("start_page"):
            if s.start_page and s.end_page:
                ranges.append((s.start_page, s.end_page))
        return ranges

    @property
    def is_fully_covered(self):
        if not self.expected_start_page or not self.expected_end_page:
            return False
        covered = set()
        for start, end in self.coverage:
            covered.update(range(start, end + 1))
        expected = set(
            range(
                self.expected_start_page,
                self.expected_end_page + 1,
            )
        )
        return expected.issubset(covered)

    def __str__(self):
        return f"{self.reporter.short_name} vol. {self.volume_number}"


def book_upload_path(instance: "Scan", filename: str) -> str:
    """Generate upload path for book scan PDFs.

    Example: ``original_scans/a3d/218/1/a3d.218.1.95.original.pdf``

    :param instance: The Scan model instance.
    :param filename: The original filename.
    :return: The upload path.
    """
    short = instance.reporter.short_name
    start = instance.start_page or 1
    end = instance.end_page or 0
    return (
        f"original_scans/{short}/{instance.volume}/{start}/"
        f"{short}.{instance.volume}.{start}.{end}.original.pdf"
    )


def compressed_upload_path(instance: "Scan", filename: str) -> str:
    """Generate upload path for compressed book scan PDFs.

    Example: ``books/f3d/compressed/42_f3d_1-200_full.pdf``

    :param instance: The Scan model instance.
    :param filename: The original filename.
    :return: The upload path.
    """
    return (
        f"books/{instance.reporter.short_name}/compressed/"
        f"{instance.volume}_{instance.reporter.short_name}"
        f"_{instance.start_page}-{instance.end_page}"
        f"_{instance.source}.pdf"
    )


def book_cover_path(instance: "Scan", filename: str) -> str:
    """Generate upload path for book cover images.

    Example: ``books/f3d/42_f3d_cover.jpg``

    :param instance: The Scan model instance.
    :param filename: The original filename.
    :return: The upload path.
    """
    ext = filename.rsplit(".", 1)[-1] if "." in filename else "jpg"
    return (
        f"books/{instance.reporter.short_name}/"
        f"{instance.volume}_{instance.reporter.short_name}_cover.{ext}"
    )


@deconstructible
class opinion_pdf_path:
    """Upload-path callable that places opinion PDFs under a typed subfolder.

    The generated filename uses the format
    ``<reporter_slug>.<volume>.<page_start>-<page_end>.<ext>``
    with page numbers zero-padded to four digits.

    Example: ``opinions/f3d/42/unredacted/f3d.42.0001-0025.pdf``

    :param subfolder: Subdirectory name (e.g. "unredacted", "redacted").
    """

    def __init__(self, subfolder: str) -> None:
        self.subfolder = subfolder

    def __call__(self, instance: "OpinionScan", filename: str) -> str:
        """Generate the upload path for the given instance and filename.

        :param instance: The OpinionScan model instance.
        :param filename: The original filename.
        :return: The upload path.
        """
        ext = filename.rsplit(".", 1)[-1] if "." in filename else "pdf"
        return (
            f"opinions/{instance.reporter.short_name}"
            f"/{instance.volume}/{self.subfolder}"
            f"/{instance.reporter.short_name}.{instance.volume}"
            f".{instance.page_start:04d}-{instance.page_end:04d}.{ext}"
        )


class Scan(AbstractDateTimeModel):
    volume_obj = models.ForeignKey(
        Volume,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="scans",
        help_text="The Volume this scan belongs to.",
    )
    reporter = models.ForeignKey(
        Reporter,
        on_delete=models.PROTECT,
        related_name="scans",
    )
    volume = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    part_label = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="Part identifier (e.g. 'A', 'B', '3' for advance sheets).",
    )
    number_of_pages = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        null=True,
        blank=True,
        help_text=(
            "Canonical page count of the printed volume, entered by the"
            " scanner. Excludes withdrawn opinions and may not equal the"
            " uploaded PDF's length when the volume contains special"
            " pages (e.g. 1390A, 1390B) or skipped page ranges. See"
            " page_count for the actual PDF length."
        ),
    )
    start_page = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        null=True,
        blank=True,
    )
    end_page = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        null=True,
        blank=True,
    )
    source = models.CharField(
        max_length=20,
        choices=Source.choices,
    )
    book_cover = models.FileField(
        upload_to=book_cover_path,
        blank=True,
        validators=[
            FileExtensionValidator(
                allowed_extensions=["pdf", "jpg", "jpeg", "gif", "png"]
            )
        ],
    )
    original_pdf = models.FileField(
        upload_to=book_upload_path,
        blank=True,
    )
    redacted_pdf = models.FileField(
        storage=_local_storage,
        null=True,
        blank=True,
    )
    compressed_pdf = models.FileField(
        upload_to=compressed_upload_path,
        storage=_local_storage,
        null=True,
        blank=True,
    )
    process_output = models.TextField(
        blank=True,
        help_text="Verbose output from blackletter pipeline processing.",
    )
    status = models.CharField(
        # 40 fits the longest value,
        # "ready_for_page_completeness_review" (34).
        max_length=40,
        choices=Status.choices,
        default=Status.UPLOADED,
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="scans",
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    queued_action = models.CharField(
        max_length=30,
        choices=QueuedAction.choices,
        blank=True,
        default="",
        help_text="Action for the daemon to run when status is queued.",
    )
    notes = models.TextField(blank=True)
    stage = models.CharField(
        max_length=20,
        choices=Stage.choices,
        default=Stage.VALIDATE,
    )
    progress_current = models.PositiveIntegerField(default=0)
    progress_total = models.PositiveIntegerField(default=0)
    progress_message = models.CharField(max_length=255, blank=True, default="")
    progress_log = models.TextField(
        blank=True,
        default="",
        help_text="Captured stdout from processing.",
    )
    retry_count = models.PositiveIntegerField(
        default=0,
        help_text="Number of transient RunPod failures before the current run.",
    )
    interruption_count = models.PositiveIntegerField(
        default=0,
        help_text=(
            "Times the daemon was killed or timed out while this scan was "
            "PROCESSING and re-queued it. Distinct from retry_count: these "
            "are infra interruptions (deploys, evictions), not scan failures."
        ),
    )
    ocr_results = models.JSONField(
        default=list,
        blank=True,
        help_text="Per-page OCR detection results.",
    )
    page_map = models.JSONField(
        default=list,
        blank=True,
        help_text="Viewer page sequence.",
    )
    missing_pages = models.JSONField(
        default=list,
        blank=True,
        help_text="List of missing logical page numbers.",
    )
    source_fingerprint = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=(
            "Identity of the original the pipeline last sharded:"
            " '{size_bytes}:{page_count}', the same pair the shard"
            " manifest records. Stamped by sharding.ensure_shards, and"
            " copied onto every PageEdit a curator writes (#214), so a"
            " re-cut or replaced original makes the edits written"
            " against the old one detectable rather than wrong."
        ),
    )
    page_count = models.PositiveIntegerField(
        default=0,
        help_text=(
            "Actual page count of the uploaded PDF file. Auto-populated"
            " by the processing pipeline and updated when pages are"
            " inserted or deleted. May differ from number_of_pages when"
            " the printed volume has special pages or skipped ranges."
        ),
    )
    redacted_pdf_path = models.CharField(
        max_length=1024, blank=True, default=""
    )
    has_state_abbrev = models.BooleanField(default=True)
    source_library = models.CharField(max_length=200, blank=True, default="")
    s3_uploaded = models.BooleanField(
        default=False,
        help_text="Whether final files have been uploaded to S3.",
    )
    s3_path = models.CharField(
        max_length=512,
        blank=True,
        default="",
        help_text="Relative S3 key prefix for approved files, e.g. approved/a3d/218/1/",
    )

    class Meta:
        indexes = [
            models.Index(
                fields=["reporter", "volume"],
                name="idx_reporter_volume",
            ),
            models.Index(fields=["status"], name="idx_status"),
            models.Index(fields=["uploaded_by"], name="idx_uploaded_by"),
            models.Index(fields=["stage"], name="idx_stage"),
        ]
        constraints = []
        ordering = ["-date_created"]

    @staticmethod
    def requeue_or_flag_interrupted(
        queryset, requeue_message, max_interruptions=None
    ):
        """Re-queue interrupted PROCESSING scans, flagging chronic offenders.

        The daemon re-queues an in-flight scan (``PROCESSING -> QUEUED``)
        without consuming its RunPod retry budget whenever it is killed
        (SIGTERM) or the scan times out mid-pipeline. That is deliberate so a
        deploy or eviction can't burn a scan's retries, but it means a scan
        can be re-queued forever if the daemon pod churns, silently redoing
        GPU work and never surfacing (issue #124).

        This bounds that loop: each call increments ``interruption_count`` and
        re-queues the scan, unless it has now been interrupted more than
        ``max_interruptions`` times, in which case it moves to
        ``ERROR_INTERRUPTED`` so a human is prompted to look instead.

        :param queryset: Scans to act on; only rows still in ``PROCESSING``
            are touched (the status guard avoids stomping a scan another
            replica or an admin action has already moved).
        :param requeue_message: ``progress_message`` for re-queued scans.
        :param max_interruptions: Interruption ceiling; defaults to
            ``settings.DAEMON_MAX_INTERRUPTIONS``.
        :return: ``(requeued, flagged)`` counts.
        :rtype: tuple[int, int]
        """
        if max_interruptions is None:
            max_interruptions = settings.DAEMON_MAX_INTERRUPTIONS

        pks = list(
            queryset.filter(status=Status.PROCESSING).values_list(
                "pk", flat=True
            )
        )
        if not pks:
            return 0, 0

        flag_message = (
            f"Interrupted {max_interruptions}+ times without completing "
            "(daemon killed or timed out mid-pipeline). Flagged for review."
        )

        # Single atomic UPDATE: increment interruption_count and branch on the
        # PRE-increment value via CASE/WHEN (no read-then-write race). A
        # pre-increment `>= max` is a post-increment `> max`, so a scan is
        # allowed `max_interruptions` re-queues before it is flagged. The
        # PROCESSING guard means we never stomp a scan already moved on.
        Scan.objects.filter(pk__in=pks, status=Status.PROCESSING).update(
            interruption_count=models.F("interruption_count") + 1,
            status=models.Case(
                models.When(
                    interruption_count__gte=max_interruptions,
                    then=models.Value(Status.ERROR_INTERRUPTED),
                ),
                default=models.Value(Status.QUEUED),
            ),
            progress_message=models.Case(
                models.When(
                    interruption_count__gte=max_interruptions,
                    then=models.Value(flag_message),
                ),
                default=models.Value(requeue_message),
            ),
        )

        flagged_pks = list(
            Scan.objects.filter(
                pk__in=pks, status=Status.ERROR_INTERRUPTED
            ).values_list("pk", flat=True)
        )
        requeued = len(pks) - len(flagged_pks)

        if requeued:
            # INFO so a routine re-queue lands as a Sentry breadcrumb (not an
            # event) and we can see how often scans get interrupted.
            logger.info(
                "Re-queued %d interrupted scan(s) for the next daemon tick.",
                requeued,
            )
        if flagged_pks:
            # ERROR so hitting the interruption ceiling raises a Sentry event:
            # the scan won't self-heal and needs a human to re-queue it.
            logger.error(
                "Flagged %d scan(s) as ERROR_INTERRUPTED after exceeding %d "
                "interruptions; needs manual re-queue: %s",
                len(flagged_pks),
                max_interruptions,
                flagged_pks,
            )
        return requeued, len(flagged_pks)

    def clean(self):
        """Validate page range and page count consistency.

        :raises ValidationError: If start_page > end_page or
            number_of_pages is less than the page range.
        """
        super().clean()
        errors = {}
        if (
            self.start_page
            and self.end_page
            and self.start_page > self.end_page
        ):
            errors["end_page"] = (
                "End page must be greater than or equal to start page."
            )
        if (
            self.number_of_pages
            and self.start_page
            and self.end_page
            and self.start_page <= self.end_page
            and self.number_of_pages < self.end_page - self.start_page + 1
        ):
            errors["number_of_pages"] = (
                "Number of pages cannot be less than the page range"
                " (end_page - start_page + 1)."
            )
        if errors:
            raise ValidationError(errors)

    def _path_suffix(self) -> Path:
        """Return the per-scan path suffix used by output_dir variants.

        :return: Relative path like ``{pk}/{reporter}/{vol}/{start}``.
        :rtype: Path
        """
        path = Path(str(self.pk))
        if self.reporter and self.volume:
            path = (
                path
                / self.reporter.short_name
                / str(self.volume)
                / str(self.start_page or 1)
            )
        return path

    @property
    def output_dir(self) -> str:
        """Return the processing directory for this scan.

        In DEVELOPMENT, uses ``MEDIA_ROOT/processed/...`` so local work
        stays in one place. In production, always uses the ephemeral
        ``PROCESSING_TMP_DIR/...`` path; S3 is the source of truth, and
        this directory is populated on upload or lazily by
        ``s3_sync.download_processing_files`` when the viewer opens.

        :return: The absolute path to the output directory.
        :rtype: str
        """
        suffix = self._path_suffix()
        if settings.DEVELOPMENT:
            return str(Path(settings.MEDIA_ROOT) / "processed" / suffix)
        return str(Path(settings.PROCESSING_TMP_DIR) / suffix)

    @property
    def pdf_path(self) -> str:
        """Return a local filesystem path to the original uploaded PDF.

        Resolution order:

        1. ``output_dir/<name>.original.pdf`` (present in DEV after
           upload, or in prod after pulling from S3).
        2. Django ``FileField.path`` if the file actually exists on disk
           (covers DEV and tests where the FileField was written to
           MEDIA_ROOT).

        Raises ``FileNotFoundError`` when no local copy exists: in prod
        this signals the caller should invoke
        ``s3_sync.download_processing_files(scan)`` first.

        :return: The filesystem path of the original PDF.
        :rtype: str
        :raises FileNotFoundError: When no local file is available.
        """
        if self.output_dir and self.original_pdf.name:
            local = Path(self.output_dir) / Path(self.original_pdf.name).name
            if local.exists():
                return str(local)
        if self.original_pdf and self.original_pdf.name:
            try:
                field_path = self.original_pdf.path
            except (ValueError, NotImplementedError):
                field_path = None
            if field_path and Path(field_path).exists():
                return field_path
        raise FileNotFoundError(
            f"scan {self.pk} has no local original PDF; "
            "pull from S3 via s3_sync.download_processing_files first"
        )

    def __str__(self):
        return (
            f"{self.reporter} vol. {self.volume} ({self.get_status_display()})"
        )


class OpinionScan(AbstractDateTimeModel):
    """An individual opinion extracted from a book scan or uploaded standalone."""

    scan = models.ForeignKey(
        Scan,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="opinions",
    )
    reporter = models.ForeignKey(
        Reporter,
        on_delete=models.PROTECT,
        related_name="opinion_scans",
    )
    volume = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    original_pdf = models.FileField(
        upload_to=opinion_pdf_path("unredacted"),
        storage=_local_storage,
        max_length=512,
    )
    redacted_pdf = models.FileField(
        upload_to=opinion_pdf_path("redacted"),
        storage=_local_storage,
        max_length=512,
        null=True,
        blank=True,
    )
    status = models.CharField(
        max_length=20,
        choices=OpinionStatus.choices,
        default=OpinionStatus.NO_STATUS,
    )
    page_start = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    page_end = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="opinion_scans",
    )
    notes = models.TextField(blank=True)
    process_output = models.TextField(
        blank=True,
        help_text="Verbose output from blackletter pipeline processing.",
    )
    opinion_order = models.PositiveIntegerField(default=0)
    caption_page_index = models.PositiveIntegerField(null=True, blank=True)
    key_page_index = models.PositiveIntegerField(null=True, blank=True)
    has_image = models.BooleanField(default=False)
    boundary = models.ForeignKey(
        "OpinionBoundary",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="opinion_scans",
        help_text=(
            "The review-2 boundary this file was cut from (issue #240, "
            "PR C). Step 3 sets it when it creates the row; a computed "
            "boundary is rebuilt at each compute, so the link stands "
            "only while the boundary row does (#165)."
        ),
    )

    class Meta:
        indexes = [
            models.Index(fields=["scan"], name="idx_opinion_scan"),
            models.Index(
                fields=["reporter", "volume"],
                name="idx_opinion_reporter_volume",
            ),
            models.Index(fields=["status"], name="idx_opinion_status"),
        ]
        ordering = ["opinion_order", "-date_created"]

    def clean(self):
        """Validate that page_start does not exceed page_end.

        :raises ValidationError: If page_start > page_end.
        """
        super().clean()
        if (
            self.page_start
            and self.page_end
            and self.page_start > self.page_end
        ):
            raise ValidationError(
                {
                    "page_end": "End page must be greater than or equal to start page."
                }
            )

    def __str__(self):
        return (
            f"{self.reporter} vol. {self.volume} opinion"
            f" ({self.get_status_display()})"
        )


class CheckName(models.TextChoices):
    """Types of validation and processing checks."""

    # Page number validation (from blackletter)
    NO_PAGE_NUMBER = "no_page_number", "No page number detected"
    MISSING_PAGE = "missing_page", "Missing page in sequence"
    DUPLICATE_PAGE = "duplicate_page", "Duplicate page number"
    BACKWARD_PAGE = "backward_page", "Page number goes backward"
    LARGE_GAP = "large_gap", "Large gap in page numbers"
    SUSPICIOUS_READING = "suspicious_reading", "Suspicious OCR reading"
    PAGE_RANGE = "page_range", "Page range detected"
    MISLABELED_DOCUMENT = "mislabeled_document", "Mislabeled document type"
    AUTO_CORRECTED = "auto_corrected", "Auto-corrected page number"
    BLANK_PAGE = "blank_page", "Blank page detected"
    ORIENTATION = "orientation", "Page orientation issue"
    STALE_PAGE_EDIT = "stale_page_edit", "Page edit not applied"

    # User actions (from scanning views)
    PROCESS_FLAG = "process_flag", "User-flagged issue"
    SUPPRESS_DETECTION = "suppress_detection", "Suppress a detection"
    ADD_DETECTION = "add_detection", "Add a detection"
    APPROVE_DETECTION = "approve_detection", "Approve a detection"


#: Checks whose ``Issue.page_number`` is a physical PDF page, 1-based.
#: Every other check names the printed page number, which repeats when
#: unnumbered front matter borrows numbers from the real pages (#90), so
#: a reader must resolve it through the page map rather than match it.
#: The viewer highlights by physical position, and a dismissal keeps the
#: address in whichever space its check uses (#214), so both need this.
PHYSICAL_PAGE_CHECKS = frozenset(
    {
        CheckName.NO_PAGE_NUMBER,
        CheckName.SUSPICIOUS_READING,
        CheckName.AUTO_CORRECTED,
        CheckName.BLANK_PAGE,
        CheckName.ORIENTATION,
        CheckName.STALE_PAGE_EDIT,
    }
)

#: The checks a page deletion answers (#255). A card about a page the
#: curator marked for deletion is noise: the page goes away, and the
#: finding goes with it. Only the physical space, because a deletion
#: names a physical page while every other check names a printed
#: number, whose cards (a duplicate, a gap) count numbers over the
#: whole volume and need the sequence analysis to run again without
#: those pages. ``STALE_PAGE_EDIT`` is excepted: it says that a
#: decision did not land, and a curator must always hear that.
CHECKS_A_DELETION_ANSWERS = PHYSICAL_PAGE_CHECKS - {CheckName.STALE_PAGE_EDIT}


class Issue(AbstractDateTimeModel):
    """A validation or processing issue found in a scan."""

    class Severity(models.TextChoices):
        ERROR = "error", "Error"
        WARNING = "warning", "Warning"
        INFO = "info", "Info"

    scan = models.ForeignKey(
        Scan,
        on_delete=models.CASCADE,
        related_name="issues",
    )
    page_number = models.PositiveIntegerField(null=True, blank=True)
    check_name = models.CharField(
        max_length=100,
        choices=CheckName.choices,
    )
    severity = models.CharField(
        max_length=10,
        choices=Severity.choices,
        default=Severity.ERROR,
    )
    message = models.TextField()
    metadata = models.JSONField(
        blank=True,
        default=dict,
        help_text="Structured data (e.g. suppression info).",
    )

    class Meta:
        ordering = ["page_number", "severity"]

    def __str__(self):
        page = f"p.{self.page_number}" if self.page_number else "doc"
        return f"[{self.severity}] {page}: {self.message}"


class DetectionQuerySet(AutoNowQuerySet):
    """The reads every consumer of the detections shares (issue #240)."""

    def live(self):
        """Return the rows a reader may act on.

        A model row that no ``deactivate`` decision hides, and a
        hand-drawn row that is not withdrawn. ``active`` is the derived
        flag both write, so one filter answers for both.

        :returns: The filtered queryset.
        """
        return self.filter(active=True)

    def model_rows(self):
        """Return the rows the model wrote, live or not.

        :returns: The filtered queryset.
        """
        return self.exclude(model_name=Detection.ModelName.MANUAL)


class Detection(AbstractDateTimeModel):
    """One bounding box on one page (YOLO, or a curator's hand).

    Coordinates are in image pixels of the 200 dpi render the model
    read, and ``img_width``/``img_height`` say how big that render was.

    **Two families of rows, and one rule for each (issue #240).** A
    model row is disposable: every import (``services._import_detections``)
    deletes the scan's model rows and writes the merged run again, so
    nothing supersedes one and nothing keeps an old one. A hand-drawn
    row (``model_name`` ``MANUAL``) is a human addition, and automation
    never deletes it; a curator takes it back with ``withdrawn_at``.

    **A curator's decision about a model row is a `DetectionDecision`**,
    not a write on the row: the row will be deleted at the next import,
    so the decision names its target by address (the source page, the
    label, a copy of the box), and the import resolves it onto the new
    row with the same box. ``decision`` is that resolution, and
    ``confidence = 1.0`` / ``active = False`` are the derived reads
    blackletter and the viewer want. Nothing writes those two by hand
    any more.

    **The address is the source page** (``source_edit``, ``source_page``),
    the document and page the apply's page map names: the original as
    uploaded, or the one-page shard of a page edit. ``page_index`` is
    the row's position in the space it was imported in, and
    ``apply_run`` says which space that is (#269); a legacy row has
    neither.
    """

    objects = DetectionQuerySet.as_manager()

    scan = models.ForeignKey(
        Scan,
        on_delete=models.CASCADE,
        related_name="detections",
    )

    class ModelName(models.TextChoices):
        """YOLO model tier or source of the detection.

        ``BL_WARM`` is the single 18-class checkpoint that replaced the
        small/medium/large trio (blackletter #73, image #194). It is
        not only a label: the confidence gates differ per model family
        (``label_confidence(label, bl_warm)``), so a row that cannot
        say which family found it is read with the legacy gates. The
        row's ``found_by`` carries the same fact per detection, and
        ``blackletter.bl_warm.rows_are_bl_warm`` is the one reader of
        it.
        """

        SMALL = "small", "Small"
        MEDIUM = "medium", "Medium"
        LARGE = "large", "Large"
        BL_WARM = "bl_warm", "bl-warm"
        MANUAL = "manual", "Manual"

    page_index = models.PositiveIntegerField(db_index=True)
    label = models.CharField(max_length=50)
    label_id = models.SmallIntegerField()
    confidence = models.FloatField()
    x0 = models.FloatField()
    y0 = models.FloatField()
    x1 = models.FloatField()
    y1 = models.FloatField()
    img_width = models.PositiveIntegerField(default=0)
    img_height = models.PositiveIntegerField(default=0)
    model_name = models.CharField(
        max_length=20,
        choices=ModelName.choices,
        blank=True,
        default="",
    )
    model_count = models.PositiveSmallIntegerField(default=1)
    found_by = models.JSONField(
        default=list,
        blank=True,
        help_text="Per-model confidence breakdown.",
    )
    active = models.BooleanField(default=True)

    source_edit = models.ForeignKey(
        "PageEdit",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="detections",
        help_text=(
            "The page edit whose one-page shard this box is on. Null "
            "means the original as uploaded (issue #240)."
        ),
    )
    source_page = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=(
            "1-based page of the source document: of the original, or "
            "of the edit's shard. Null on a row imported before #240."
        ),
    )
    source_fingerprint = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=(
            "The scan's source fingerprint at import. Blank on a legacy "
            "row, which matches anything."
        ),
    )
    apply_run = models.ForeignKey(
        "ApplyRun",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="detections",
        help_text=(
            "The apply run whose final page space ``page_index`` is in "
            "(#269). Null on a legacy row and on a hand-drawn row of "
            "a volume with no run: the original's space."
        ),
    )
    detect_run = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="The detection run (``ExternalJob.run``) that found it.",
    )
    decision = models.ForeignKey(
        "DetectionDecision",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="detections",
        help_text=(
            "The standing curator decision resolved onto this model row: "
            "the reason its confidence is 1.0 or it is inactive."
        ),
    )
    replaces = models.ForeignKey(
        "DetectionDecision",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="replacements",
        help_text=(
            "Hand-drawn rows only: the deactivation this box was drawn "
            "in place of, when a curator moved a model box."
        ),
    )
    withdrawn_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Hand-drawn rows only: when the curator took the box back. "
            "The row stays; ``active`` reads False."
        ),
    )
    withdrawn_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="withdrawn_detections",
        help_text="Who took the box back. Null while it stands.",
    )

    class Meta:
        ordering = ["page_index", "y0", "x0"]
        indexes = [
            models.Index(
                fields=["scan", "page_index"],
                name="idx_det_scan_page",
            ),
            models.Index(
                fields=["scan", "source_edit", "source_page"],
                name="idx_det_scan_source",
            ),
            models.Index(
                fields=["scan", "label"],
                name="idx_det_scan_label",
            ),
            models.Index(
                fields=["scan", "active"],
                name="idx_det_scan_active",
            ),
        ]

    def __str__(self):
        state = "" if self.active else " [suppressed]"
        return (
            f"{self.label} p.{self.page_index}"
            f" conf={self.confidence:.2f}{state}"
        )


class DetectionDecision(AbstractDateTimeModel):
    """One curator decision about one model detection (issue #240).

    The model rows are deleted and written again at every import, so a
    decision cannot point at one. It names its target by **address**
    instead: the source page (``source_edit``, ``source_page``), the
    label, and a copy of the box as the model drew it when the curator
    decided (``target_*``). After each import
    ``detections.resolve`` looks for the new model row on that page
    with that label whose box overlaps the copy (IoU at least
    ``detections.IOU_THRESHOLD``), sets ``Detection.decision`` on it,
    and writes the derived read: ``confidence = 1.0`` for an approval,
    ``active = False`` for a deactivation. A decision that finds no row
    is stale, and is logged; #240 PR D raises it as an issue.

    Never deleted by automation. A curator takes one back with
    ``withdrawn_at``, which also gives the row back its own values. A
    later decision on the same row withdraws the earlier one, so one
    decision stands per target.

    A curator who *moves* a model box makes two rows: a deactivation
    here, and a hand-drawn ``Detection`` that names it in ``replaces``.
    """

    class Kind(models.TextChoices):
        APPROVE = "approve", "Approve (confidence 1.0)"
        DEACTIVATE = "deactivate", "Deactivate"

    scan = models.ForeignKey(
        Scan,
        on_delete=models.CASCADE,
        related_name="detection_decisions",
    )
    kind = models.CharField(max_length=12, choices=Kind.choices)
    source_edit = models.ForeignKey(
        "PageEdit",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="detection_decisions",
        help_text="The page edit whose shard holds the page; null = the original.",
    )
    source_page = models.PositiveIntegerField(
        help_text="1-based page of the source document.",
    )
    source_fingerprint = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=(
            "The scan's source fingerprint when the decision was made. "
            "Blank matches anything."
        ),
    )
    label = models.CharField(max_length=50)
    label_id = models.SmallIntegerField()
    target_x0 = models.FloatField()
    target_y0 = models.FloatField()
    target_x1 = models.FloatField()
    target_y1 = models.FloatField()
    img_width = models.PositiveIntegerField(default=0)
    img_height = models.PositiveIntegerField(default=0)
    target_confidence = models.FloatField(
        null=True,
        blank=True,
        help_text=(
            "The model's own confidence when the decision was made, so "
            "a withdrawn approval gives it back."
        ),
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="detection_decisions",
    )
    withdrawn_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="When the curator took the decision back. Never rewritten.",
    )
    withdrawn_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="withdrawn_detection_decisions",
    )

    class Meta:
        ordering = ["scan", "source_page", "target_y0", "target_x0"]
        indexes = [
            models.Index(
                fields=["scan", "withdrawn_at"],
                name="idx_det_decision_scan_open",
            ),
        ]

    @property
    def target_bbox(self) -> list[float]:
        """The copied box, in the ``[x0, y0, x1, y1]`` shape every reader uses.

        :returns: The box.
        """
        return [self.target_x0, self.target_y0, self.target_x1, self.target_y1]

    def __str__(self):
        state = " [withdrawn]" if self.withdrawn_at else ""
        return f"{self.kind} {self.label} src p.{self.source_page}{state}"


class OpinionBoundaryQuerySet(AutoNowQuerySet):
    """The reads every consumer of the opinion boundaries shares (#240)."""

    def computed(self):
        """Return the rows the pairing wrote.

        :returns: The filtered queryset.
        """
        return self.filter(origin=OpinionBoundary.Origin.COMPUTED)

    def human(self):
        """Return the rows a curator wrote, withdrawn or not.

        :returns: The filtered queryset.
        """
        return self.filter(origin=OpinionBoundary.Origin.HUMAN)

    def standing_dismissals(self):
        """Return the curator's dismissals that are not withdrawn.

        :returns: The filtered queryset.
        """
        return self.filter(
            origin=OpinionBoundary.Origin.HUMAN,
            kind=OpinionBoundary.Kind.DISMISS,
            withdrawn_at__isnull=True,
        )

    def standing_additions(self):
        """Return the boundaries a curator added and did not take back.

        :returns: The filtered queryset.
        """
        return self.filter(
            origin=OpinionBoundary.Origin.HUMAN,
            kind=OpinionBoundary.Kind.ADD,
            withdrawn_at__isnull=True,
        )


class OpinionBoundary(AbstractDateTimeModel):
    """One opinion of a volume, or one curator decision about one (#240, PR C).

    A boundary is two **anchors**: the start is the top-left corner of
    the case caption, the end the bottom-right corner of the key icon
    that closes the opinion. Each anchor is a point in PDF points on a
    **source page** (``*_source_edit``, ``*_source_page``: the original
    as uploaded, or the one-page shard of a page edit -- the address the
    apply's page map names, the rule of ``Detection``), and its
    position in the space the compute measured in (``*_page_index``,
    0-based, in the space of ``apply_run``). The address survives a new
    apply run; the index is what the viewer draws.

    **Two families of rows, the rule of the detections.** A *computed*
    row (``origin`` ``COMPUTED``) is written by the pairing
    (``boundaries.write_computed``), and every compute deletes the
    scan's computed rows and writes them again. A *human* row (``origin``
    ``HUMAN``) is one curator decision, and automation never deletes
    it: an ``ADD`` is a boundary the curator drew, a ``DISMISS`` is a
    decision about a computed boundary. A curator takes a human row
    back with ``withdrawn_at`` (#232).

    **A dismissal names its target by its anchors**, in the anchor
    columns, because the computed row it was made on is gone at the
    next compute. ``boundaries.resolve`` then lands it on the new
    computed row with the same start address whose start anchor is
    within ``boundaries.ANCHOR_TOLERANCE_PT``, and sets ``decision`` on
    that row. A move of an anchor is a ``DISMISS`` plus an ``ADD`` that
    names it in ``replaces``, so withdrawing the addition gives the
    computed boundary back.

    ``start_detection`` and ``end_detection`` are the caption and key
    rows the pairing used, or the boxes the curator picked. They are
    ``SET_NULL``: the model rows are deleted at every import, and the
    anchors carry the position on their own.
    """

    objects = OpinionBoundaryQuerySet.as_manager()

    class Origin(models.TextChoices):
        COMPUTED = "computed", "Computed by the pairing"
        HUMAN = "human", "Made by a curator"

    class Kind(models.TextChoices):
        ADD = "add", "A boundary the curator added"
        DISMISS = "dismiss", "A computed boundary the curator dismissed"

    scan = models.ForeignKey(
        Scan,
        on_delete=models.CASCADE,
        related_name="opinion_boundaries",
    )
    origin = models.CharField(max_length=10, choices=Origin.choices)
    kind = models.CharField(
        max_length=10,
        choices=Kind.choices,
        blank=True,
        default="",
        help_text="Human rows only; blank on a computed row.",
    )

    start_source_edit = models.ForeignKey(
        "PageEdit",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="boundary_starts",
        help_text=(
            "The page edit whose shard holds the start page; null = "
            "the original as uploaded."
        ),
    )
    start_source_page = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=(
            "1-based page of the start's source document. Null when the "
            "map held no address for the page at write time."
        ),
    )
    start_page_index = models.PositiveIntegerField(
        help_text="0-based page of the start, in the space of ``apply_run``."
    )
    start_x = models.FloatField(help_text="PDF points: the caption's left.")
    start_y = models.FloatField(help_text="PDF points: the caption's top.")

    end_source_edit = models.ForeignKey(
        "PageEdit",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="boundary_ends",
        help_text="As ``start_source_edit``, for the end page.",
    )
    end_source_page = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="As ``start_source_page``, for the end page.",
    )
    end_page_index = models.PositiveIntegerField(
        help_text="0-based page of the end, in the space of ``apply_run``."
    )
    end_x = models.FloatField(help_text="PDF points: the key icon's right.")
    end_y = models.FloatField(help_text="PDF points: the key icon's bottom.")

    source_fingerprint = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=(
            "The scan's source fingerprint when the row was written. "
            "Blank matches anything (the #214 rule)."
        ),
    )
    apply_run = models.ForeignKey(
        "ApplyRun",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="opinion_boundaries",
        help_text=(
            "The apply run whose final page space the two indexes are "
            "in (#269). Null for the original's space."
        ),
    )
    detect_run = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text=(
            "Computed rows: the detection run (``ExternalJob.run``) the "
            "pairing read."
        ),
    )
    start_detection = models.ForeignKey(
        Detection,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="opinion_starts",
        help_text="The caption row the start anchor was taken from.",
    )
    end_detection = models.ForeignKey(
        Detection,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="opinion_ends",
        help_text="The key icon row the end anchor was taken from.",
    )
    ordinal = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=(
            "Computed rows: the position the pairing gave the opinion, "
            "0-based, in reading order. Null on a human row."
        ),
    )
    uncovered_page_indexes = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "The pages of this opinion, in the space of ``apply_run``, "
            "with a confident HEADNOTE box no headnote rect covers. "
            "Stamped by the redaction compute (#240 PR B), which measures "
            "it in the render's pixels where the detections and the "
            "rects both are; a request cannot, since the redaction rows "
            "are in points. PR D makes it a finding."
        ),
    )
    decision = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="decided_boundaries",
        help_text=(
            "Computed rows: the standing dismissal resolved onto this "
            "row. Null while the boundary stands."
        ),
    )
    replaces = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="replacements",
        help_text=(
            "Additions only: the dismissal this boundary was made in "
            "place of, when a curator moved an anchor."
        ),
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="opinion_boundaries",
        help_text="Human rows: the curator.",
    )
    withdrawn_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Human rows only: when the curator took the row back. "
            "Never rewritten."
        ),
    )
    withdrawn_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="withdrawn_opinion_boundaries",
        help_text="Who took the row back. Null while it stands.",
    )

    class Meta:
        ordering = ["scan", "start_page_index", "start_y", "start_x"]
        indexes = [
            models.Index(
                fields=["scan", "origin", "withdrawn_at"],
                name="idx_opb_scan_open",
            ),
            models.Index(
                fields=["scan", "start_source_edit", "start_source_page"],
                name="idx_opb_scan_start_source",
            ),
            models.Index(
                fields=["scan", "start_page_index"],
                name="idx_opb_scan_start_index",
            ),
        ]
        constraints = [
            # A computed row has no kind; a human row has one of the two.
            models.CheckConstraint(
                condition=(
                    models.Q(origin="computed", kind="")
                    | models.Q(origin="human", kind__in=["add", "dismiss"])
                ),
                name="opinion_boundary_kind_matches_origin",
            ),
        ]

    @property
    def is_computed(self) -> bool:
        return self.origin == self.Origin.COMPUTED

    @property
    def is_dismissed(self) -> bool:
        """Whether a standing dismissal hides this computed row.

        The FK is cleared when the dismissal is withdrawn, so the FK
        alone answers.
        """
        return self.decision_id is not None

    @property
    def start_address(self) -> tuple[int | None, int | None]:
        return self.start_source_edit_id, self.start_source_page

    @property
    def end_address(self) -> tuple[int | None, int | None]:
        return self.end_source_edit_id, self.end_source_page

    def __str__(self):
        what = self.kind or "opinion"
        state = " [withdrawn]" if self.withdrawn_at else ""
        return (
            f"{what} p.{self.start_page_index + 1}-{self.end_page_index + 1}"
            f"{state}"
        )


class RedactionQuerySet(AutoNowQuerySet):
    """The reads every consumer of the redactions shares (issue #240)."""

    def computed(self):
        """Return the rows the compute wrote.

        :returns: The filtered queryset.
        """
        return self.filter(origin=Redaction.Origin.COMPUTED)

    def human(self):
        """Return the standing human rows: additions and dismissals.

        :returns: The filtered queryset.
        """
        return self.filter(
            origin=Redaction.Origin.HUMAN, withdrawn_at__isnull=True
        )

    def visible(self):
        """Return the boxes a reader paints.

        A computed row under no standing dismissal, and a human ``add``
        that is not withdrawn. A ``dismiss`` row has no box of its own.

        :returns: The filtered queryset.
        """
        from django.db.models import Q

        return self.filter(
            Q(origin=Redaction.Origin.COMPUTED, decision__isnull=True)
            | Q(
                origin=Redaction.Origin.HUMAN,
                kind=Redaction.Kind.ADD,
                withdrawn_at__isnull=True,
            )
        )


class Redaction(AbstractDateTimeModel):
    """One box to paint over the volume, or one decision about such a box
    (issue #240, PR B). Replaces ``Scan.redaction_rects`` and
    ``Scan.margin_rects``.

    **Coordinates are PDF points** on the page. blackletter measures the
    redaction rects in pixels of its 200 dpi render, and the compute
    converts them once with the page scale it holds; the margin strips
    are in points already. So a reader needs the page alone, and the
    viewer scales a box by the pdf.js viewport as it scales the strips.

    **Two families of rows, one rule for each**, the rule the detections
    follow (PR A):

    - A **computed** row is disposable. Each compute deletes the scan's
      computed rows and writes them again: a better computation may find
      one box where it found two, and a kept old row would sit beside
      the new one as a duplicate. A margin strip is a computed row with
      ``rect_type`` ``margin`` and a white fill.
    - A **human** row is never deleted by automation. It is an ``add``
      (a box the curator drew, or drew in place of a computed one) or a
      ``dismiss`` (a computed box the curator took out). A curator takes
      a human row back with ``withdrawn_at``.

    **A dismiss names its target by address, not by FK**, because the
    computed row it was made on is deleted at the next compute: the
    source page, the ``rect_type``, and a copy of the computed box
    (``target_*``). After each compute ``redactions.resolve`` lands every
    standing dismiss on the new computed row at that address whose box
    overlaps the copy (IoU at least ``detections.IOU_THRESHOLD``), and
    sets ``decision`` on it, which hides it. A dismiss that lands on
    nothing is logged; PR D raises it as an issue.

    **A move of a computed box is a dismiss plus an add** that names the
    dismiss in ``replaces``, so the drawn box stands whatever a later
    compute finds, and withdrawing it gives the computed box back.

    **The address is the source page** (``source_edit``, ``source_page``)
    the apply's page map names, as on ``Detection``; ``page_index`` is
    the row's position in the space of ``apply_run``, and the compute
    moves the human rows through the new map after each run
    (``detections.relocate_rows``).
    """

    class Origin(models.TextChoices):
        COMPUTED = "computed", "Computed"
        HUMAN = "human", "Human"

    class Kind(models.TextChoices):
        ADD = "add", "Add a box"
        DISMISS = "dismiss", "Dismiss a computed box"

    class Fill(models.TextChoices):
        BLACK = "black", "Black"
        WHITE = "white", "White"

    #: The ``rect_type`` of a margin strip and of a drawn box. Every
    #: other value is blackletter's (``headnote``, ``KEY_ICON``, ...).
    MARGIN_TYPE = "margin"
    MANUAL_TYPE = "manual"

    objects = RedactionQuerySet.as_manager()

    scan = models.ForeignKey(
        Scan,
        on_delete=models.CASCADE,
        related_name="redactions",
    )
    origin = models.CharField(max_length=10, choices=Origin.choices)
    kind = models.CharField(
        max_length=10,
        choices=Kind.choices,
        blank=True,
        default="",
        help_text="Human rows only; blank on a computed row.",
    )
    rect_type = models.CharField(
        max_length=50,
        help_text=(
            "blackletter's type of the box (headnote, a label name, ...), "
            "'margin' for a strip, 'manual' for a drawn box."
        ),
    )
    fill = models.CharField(max_length=5, choices=Fill.choices)
    x0 = models.FloatField(null=True, blank=True)
    y0 = models.FloatField(null=True, blank=True)
    x1 = models.FloatField(null=True, blank=True)
    y1 = models.FloatField(null=True, blank=True)
    target_x0 = models.FloatField(null=True, blank=True)
    target_y0 = models.FloatField(null=True, blank=True)
    target_x1 = models.FloatField(null=True, blank=True)
    target_y1 = models.FloatField(null=True, blank=True)
    replaces = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="replacements",
        help_text=(
            "Adds only: the dismiss this box was drawn in place of, when "
            "a curator moved a computed box."
        ),
    )
    source_edit = models.ForeignKey(
        "PageEdit",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="redactions",
        help_text="The page edit whose shard holds the page; null = the original.",
    )
    source_page = models.PositiveIntegerField(
        help_text="1-based page of the source document.",
    )
    source_fingerprint = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="The scan's source fingerprint when the row was written.",
    )
    page_index = models.PositiveIntegerField(
        db_index=True,
        help_text="0-based position in the space of ``apply_run``.",
    )
    apply_run = models.ForeignKey(
        "ApplyRun",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="redactions",
        help_text="The apply run whose final page space ``page_index`` is in.",
    )
    detect_run = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="Computed rows: the detection run the geometry came from.",
    )
    decision = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="decided",
        help_text=(
            "Computed rows: the standing dismiss resolved onto this box, "
            "which hides it."
        ),
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="redactions",
    )
    withdrawn_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Human rows: when the curator took the row back.",
    )
    withdrawn_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="withdrawn_redactions",
    )

    class Meta:
        ordering = ["scan", "page_index", "y0", "x0"]
        indexes = [
            models.Index(
                fields=["scan", "page_index"],
                name="idx_redaction_scan_page",
            ),
            models.Index(
                fields=["scan", "source_edit", "source_page"],
                name="idx_redaction_scan_source",
            ),
            models.Index(
                fields=["scan", "origin", "withdrawn_at"],
                name="idx_redaction_scan_open",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(origin="computed", kind="")
                    | models.Q(origin="human", kind__in=["add", "dismiss"])
                ),
                name="redaction_kind_matches_origin",
            ),
            # One standing box in place of one dismissed computed box: a
            # second move in flight must write on it, not beside it.
            models.UniqueConstraint(
                fields=["replaces"],
                condition=models.Q(kind="add", withdrawn_at__isnull=True),
                name="uniq_standing_replacement_per_dismiss",
            ),
        ]

    @property
    def bbox(self) -> list[float] | None:
        """The box, ``[x0, y0, x1, y1]`` in points, or None on a dismiss.

        :returns: The box.
        """
        if self.x0 is None:
            return None
        return [self.x0, self.y0, self.x1, self.y1]

    @property
    def target_bbox(self) -> list[float] | None:
        """The copied computed box of a dismiss, or None.

        :returns: The box.
        """
        if self.target_x0 is None:
            return None
        return [self.target_x0, self.target_y0, self.target_x1, self.target_y1]

    def __str__(self):
        what = self.kind or self.rect_type
        state = " [withdrawn]" if self.withdrawn_at else ""
        return f"{self.origin} {what} p.{self.page_index}{state}"


def page_edit_image_path(instance: "PageEdit", filename: str) -> str:
    """Return the storage key of one page edit's image.

    The image goes under the scan's own processing prefix, in
    ``page_edits/``, beside ``shards/`` and ``jobs/`` (issue #214).
    Three reasons, and all three are about the apply that reads it:

    - The default storage is S3 in production, so the file outlives the
      web pod that took the upload. ``PageInsert`` used
      ``LocalProcessingStorage``, so an insert lost its image to the
      next preemption and a second pod could not read it at all.
    - The key is presignable, so the apply (#206) hands the image to
      doctor and to RunPod as a one-page shard, with the helpers every
      other stage input already uses.
    - The prefix is the scan's, so the admin scan deletion sweeps these
      objects with the two it already sweeps.

    The name carries a UUID, not the page address: an address is a
    column, and a curator who replaces an image must not overwrite the
    object an in-flight apply is reading.

    :param instance: The PageEdit the image belongs to.
    :param filename: The name the browser sent, read for its extension
        only.
    :returns: The storage key, relative to the default storage.
    :rtype: str
    """
    from scanning import s3_sync

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "png"
    ext = "".join(c for c in ext if c.isalnum())[:8] or "png"
    return (
        f"{s3_sync.s3_processing_prefix(instance.scan)}"
        f"{s3_sync.PAGE_EDITS_SUBDIR}{uuid.uuid4().hex}.{ext}"
    )


class PageEdit(AbstractDateTimeModel):
    """One decision a person made about one page of a scan (issue #214).

    Review 1 asks a curator four kinds of question about a volume, and
    the portal used to answer them in three different ways: a page
    number inside the ``Scan.ocr_results`` JSON blob, a
    ``PageDeletion`` row addressed by PDF page, a ``PageInsert`` row
    addressed by printed page number, and nothing at all for a
    replacement. This model is the one home for all of them, plus the
    dismissal of an issue, which used to be a ``DELETE`` of the derived
    ``Issue`` row and so did not survive one recompute.

    A row's **address** is the page it is about: which page of the
    volume the curator was looking at. Two columns carry it --
    ``pdf_page`` for a page, ``anchor_pdf_page`` for the gap between
    two pages -- and nothing else on the row locates anything.

    What must not be broken:

    - **Every address is in the physical space of the original as it
      was uploaded**, 1-based, the space ``Detection.page_index`` and
      the shard manifest already use. No stored address ever names a
      page of an edited document.
    - **An insert is addressed by a gap, not by a page.**
      ``anchor_pdf_page`` is the original page the image follows, and 0
      means "before page 1". ``ordinal`` orders several images in one
      gap. The anchor is resolved once, when the curator uploads the
      image; ``logical_page`` is the printed number beside it, a label
      only. A printed number cannot be an address: front matter has
      none, and two pages can both print 1074 -- which is one of the
      defects review 1 exists to find.
    - **A decision stands until it is withdrawn, and it is never
      rewritten or deleted.** A curator who takes the decision back
      stamps ``withdrawn_at`` and ``withdrawn_by`` (#232), and that is
      the one stamp that closes a row. The apply (#224) stamps
      ``applied_at`` and ``applied_run`` when it builds the decision
      into a final volume, but the row keeps standing: a reopened
      review must show an applied deletion as deleted, and the next
      apply run must build it again, or the second final PDF would
      restore the page in silence. So every unique constraint is
      partial over the standing rows -- one decision per address --
      and a curator who edits the same page again supersedes the row
      there (``page_edits.supersede``): an open row is updated in
      place, an applied row is withdrawn and a new one is written. A
      second file uploaded for one page withdraws the first row rather
      than writing over it -- the audit must show every file a person
      uploaded, and the object of an overwritten row would stay in the
      bucket with nothing naming it.
    - **``source_fingerprint`` is the scan's**
      (``Scan.source_fingerprint``, size plus page count, the identity
      the shard manifest trusts). A replaced or re-cut original makes
      the edits written against the old one detectable, instead of
      silently wrong. A blank value is a legacy row, from before the
      field existed, and matches anything.

    :cvar Kind: What the curator decided. One kind per decision, so the
        apply reads a decision in one step: a replacement is *not* a
        delete beside an insert, which would need two addresses in two
        spaces to say "this image stands where that page stood", and
        two undos to take back.
    """

    class Kind(models.TextChoices):
        """The decisions review 1 can record about a page."""

        SET_NUMBER = "set_number", "Set the printed page number"
        DELETE_PAGE = "delete_page", "Delete a page"
        INSERT_PAGE = "insert_page", "Insert a page image"
        REPLACE_PAGE = "replace_page", "Replace a page with an image"
        ROTATE_PAGE = "rotate_page", "Rotate a page"
        DISMISS_ISSUE = "dismiss_issue", "Dismiss an issue"

    #: Kinds that change what the volume is, so the apply (#206) must
    #: run before the change is real. These are what
    #: ``has_pending_changes`` counts. A number, and a dismissal, need
    #: no apply: the issue rebuild overlays them on every pass.
    STRUCTURAL_KINDS = (
        Kind.DELETE_PAGE,
        Kind.INSERT_PAGE,
        Kind.REPLACE_PAGE,
        Kind.ROTATE_PAGE,
    )

    scan = models.ForeignKey(
        Scan,
        on_delete=models.CASCADE,
        related_name="page_edits",
    )
    kind = models.CharField(
        max_length=32,
        choices=Kind.choices,
        db_index=True,
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="page_edits",
        help_text=(
            "Who decided. Null on a row the #214 data migration wrote, "
            "since the storage it read kept no author."
        ),
    )

    pdf_page = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=(
            "1-based page of the original PDF this decision is about. "
            "Set for every kind but an insert; null on a dismissal of "
            "an issue that names a printed page number or the whole "
            "volume rather than a physical page."
        ),
    )
    anchor_pdf_page = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=(
            "Inserts only: the 1-based original page the image "
            "follows. 0 puts the image before page 1."
        ),
    )
    ordinal = models.PositiveSmallIntegerField(
        default=0,
        help_text="Inserts only: the order of several images in one gap.",
    )

    value = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text=(
            "What was decided: the printed number ('1075') or range "
            "('678-686') for a number, blank when the curator cleared "
            "it; the rotation in degrees; the dismissed check's name."
        ),
    )
    previous_value = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text=(
            "The page number this row overruled, on a SET_NUMBER row: "
            "what the model had read on that page ('677'), or blank "
            "when it had read nothing. Blank on every other kind. The "
            "model's reading is rebuilt from the OCR run on every "
            "recompute, so this is the only record that a person "
            "disagreed with it."
        ),
    )
    logical_page = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text=(
            "The page number printed on the page, kept as a label and "
            "for the audit: the number an insert's placeholder showed, "
            "or the number a dismissed issue named. It never locates "
            "the page -- front matter prints no number, and two pages "
            "can both print 1074, so pdf_page and anchor_pdf_page are "
            "what a reader follows."
        ),
    )

    image = models.FileField(
        upload_to=page_edit_image_path,
        blank=True,
        help_text="Inserts and replacements: the page the curator uploaded.",
    )

    source_fingerprint = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=(
            "The scan's source fingerprint when the decision was made. "
            "Blank on a legacy row, which matches anything."
        ),
    )
    applied_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When the apply (#224) built this decision into a final "
            "volume. A ledger stamp, not a close: the row keeps "
            "standing until it is withdrawn, and it is never rewritten."
        ),
    )
    applied_run = models.ForeignKey(
        "ApplyRun",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="applied_edits",
        help_text=(
            "The apply run whose final volume carries this decision. "
            "Null while no build has read the row."
        ),
    )
    withdrawn_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text=(
            "When the decision was taken back: the curator undid it, "
            "or replaced it with a later one (#232). A stamped row is "
            "history too, and it is never rewritten either."
        ),
    )
    withdrawn_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="withdrawn_page_edits",
        help_text="Who took the decision back. Null while it stands.",
    )

    class Meta:
        ordering = ["scan", "pdf_page", "anchor_pdf_page", "ordinal"]
        indexes = [
            models.Index(
                fields=["scan", "kind", "applied_at"],
                name="idx_page_edit_scan_kind",
            ),
        ]
        constraints = [
            # One address column per kind, so a null is never a second
            # meaning of a column. An insert lives in a gap; every
            # other kind lives on a page.
            models.CheckConstraint(
                condition=(
                    models.Q(
                        kind="insert_page",
                        pdf_page__isnull=True,
                        anchor_pdf_page__isnull=False,
                    )
                    | models.Q(
                        kind="dismiss_issue",
                        anchor_pdf_page__isnull=True,
                    )
                    | models.Q(
                        kind__in=(
                            "set_number",
                            "delete_page",
                            "replace_page",
                            "rotate_page",
                        ),
                        pdf_page__isnull=False,
                        anchor_pdf_page__isnull=True,
                    )
                ),
                name="page_edit_address_matches_kind",
            ),
            # A dismissal names the check it dismisses; the rebuild
            # matches on that name, since it gives every Issue row it
            # rebuilds a new primary key.
            models.CheckConstraint(
                condition=(
                    ~models.Q(kind="dismiss_issue") | ~models.Q(value="")
                ),
                name="page_edit_dismissal_names_a_check",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(kind="rotate_page")
                    | models.Q(value__in=PAGE_EDIT_ROTATIONS)
                ),
                name="page_edit_rotation_is_a_quarter_turn",
            ),
            # The unique keys are partial over the standing rows: one
            # decision per address. A row leaves that set in one way
            # only, the curator taking it back (#232). The apply stamp
            # is not a close (#224): an applied row stands, and the
            # curator who edits that page again supersedes it, so the
            # audit keeps both rows and the address keeps one decision.
            models.UniqueConstraint(
                fields=["scan", "kind", "pdf_page"],
                condition=(
                    models.Q(withdrawn_at__isnull=True)
                    & models.Q(pdf_page__isnull=False)
                    & ~models.Q(kind="dismiss_issue")
                ),
                name="uniq_standing_page_edit_per_page",
            ),
            # A page raises several checks, so a dismissal is unique
            # per check, not per page. Both address columns are in the
            # key because an issue names a page in one of two spaces: a
            # physical one (``no_page_number`` on PDF page 7) or a
            # printed one (``missing_page`` 1074), and the rebuild
            # compares whichever the check uses.
            # ``nulls_distinct=False`` makes the key hold for the
            # volume-level dismissals too, whose ``pdf_page`` is null.
            models.UniqueConstraint(
                fields=[
                    "scan",
                    "kind",
                    "pdf_page",
                    "logical_page",
                    "value",
                ],
                condition=(
                    models.Q(withdrawn_at__isnull=True)
                    & models.Q(kind="dismiss_issue")
                ),
                nulls_distinct=False,
                name="uniq_standing_dismissal_per_check",
            ),
            models.UniqueConstraint(
                fields=["scan", "anchor_pdf_page", "ordinal"],
                condition=(
                    models.Q(withdrawn_at__isnull=True)
                    & models.Q(kind="insert_page")
                ),
                name="uniq_standing_insert_per_gap",
            ),
        ]

    def __str__(self):
        where = (
            f"after p.{self.anchor_pdf_page}"
            if self.kind == self.Kind.INSERT_PAGE
            else f"p.{self.pdf_page}"
        )
        value = f" = {self.value!r}" if self.value else ""
        state = ""
        if self.withdrawn_at is not None:
            state = " [withdrawn]"
        elif self.applied_at is not None:
            state = " [applied]"
        return f"{self.get_kind_display()} {where}{value}{state}"


class ApplyRun(AbstractDateTimeModel):
    """One build of the final volume from the original plus the page
    edits (issue #224), ``a{number}`` in the S3 keys.

    Review 1 ends when a curator approves the page completeness. The
    ``PageEdit`` rows plus the original then describe the complete
    volume, and this row records one attempt to build it and to glue
    the paid per-shard results into its page space. **The apply
    assembles; it does not recompute.** A page nobody touched keeps its
    conversion, its OCR read and its detections; only a page a curator
    added or changed enters a queue, as a one-page shard whose
    ``ExternalJob`` rows point back here through ``apply_run``.

    Why a row of its own, rather than a mark on a job row like the
    glue and apply ledgers of the volume stages:

    - A run may have **no job rows** at all -- a volume with only
      deletes, or with no structural edit -- so there is no shard-0
      row to carry a ledger.
    - One run spans **three stages** whose glues finish at different
      times, and a mark on one stage's head row cannot say which of
      the three is written.
    - The trigger asks every 15 seconds, for every approved scan, "is
      there a run for this edit set, is it built, which glues are
      written, how many attempts are spent". That is one query here,
      and five S3 HEADs otherwise.

    The offset map is stored here **once**, at build time, and every
    glue reads it. Nothing derives it again. The original stays the
    source of record: ``source_fingerprint`` is copied from the scan
    so a glue can refuse a document from another original, and the
    apply never writes ``Scan.source_fingerprint``.

    A run with no structural edit aliases the review-1 artifacts: its
    ``final_pdf_key`` is the original's key and its ``bitonal_key``
    the volume ``bitonal.pdf``, with no copy. The printed-page map is
    written for every run, because it is a product.
    """

    scan = models.ForeignKey(
        Scan,
        on_delete=models.CASCADE,
        related_name="apply_runs",
    )
    number = models.PositiveSmallIntegerField(
        help_text="The n in a{n}: 1 for the first build of this scan.",
    )
    source_fingerprint = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=(
            "The scan's source fingerprint when the run was built. "
            "Every glue checks its inputs against it."
        ),
    )
    page_map = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "The offset map, written once at build time: one entry per "
            "final page naming its source (an original page, with its "
            "rotation, or a page of an edit's file), plus the deleted "
            "pages and the original's page count."
        ),
    )
    edit_ids = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "The standing structural PageEdit rows the build read, in "
            "primary-key order. The trigger compares it with the "
            "current set to decide whether a new run is due."
        ),
    )
    final_pdf_key = models.CharField(
        max_length=1024,
        blank=True,
        default="",
        help_text=(
            "S3 key of the final PDF, or of the original when no "
            "structural edit exists. Blank until the build commits."
        ),
    )
    bitonal_key = models.CharField(
        max_length=1024,
        blank=True,
        default="",
        help_text="S3 key of the final bitonal copy. Blank until glued.",
    )
    ocr_key = models.CharField(
        max_length=1024,
        blank=True,
        default="",
        help_text="S3 key of the final OCR volume JSON. Blank until glued.",
    )
    printed_pages_key = models.CharField(
        max_length=1024,
        blank=True,
        default="",
        help_text=(
            "S3 key of the frozen printed-page map, in the final page "
            "space. Blank until the OCR glue writes it."
        ),
    )
    detections_key = models.CharField(
        max_length=1024,
        blank=True,
        default="",
        help_text=(
            "S3 key of the final detections volume JSON. Blank until "
            "glued, which waits for a volume detection run."
        ),
    )
    built_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the final PDF, the map and the job rows were committed.",
    )
    superseded_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When a later run replaced this one: the review was "
            "reopened, or an admin gave up on a dead row. Its outputs "
            "stay in S3."
        ),
    )
    attempts = models.PositiveSmallIntegerField(
        default=0,
        help_text="Failed attempts at the current phase.",
    )
    last_error = models.TextField(
        blank=True,
        default="",
        help_text="What the last failed attempt raised.",
    )
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    dead_row_noted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When the trigger first saw a dead job row on this run and "
            "logged it. Its own stamp: last_error is written by the "
            "failed attempts and cleared by a successful glue, so a note "
            "kept there was repeated after every glue and lost behind a "
            "failed one."
        ),
    )

    class Meta:
        ordering = ["scan", "number"]
        constraints = [
            models.UniqueConstraint(
                fields=["scan", "number"],
                name="uniq_apply_run_number_per_scan",
            ),
        ]

    def __str__(self):
        return f"Apply run a{self.number} of scan {self.scan_id}"

    @property
    def label(self) -> str:
        """Return the run's name in the S3 keys and the logs.

        :returns: ``a{number}``.
        :rtype: str
        """
        return f"a{self.number}"

    @property
    def is_built(self) -> bool:
        """Return whether phase 1 committed."""
        return self.built_at is not None

    @property
    def is_glued(self) -> bool:
        """Return whether the review-1 glues are written.

        The bitonal copy, the OCR volume and the printed pages. The
        detections glue waits for a volume detection run, so it is not
        part of this; :attr:`is_complete` is the whole set.
        """
        return bool(
            self.bitonal_key and self.ocr_key and self.printed_pages_key
        )

    @property
    def is_complete(self) -> bool:
        """Return whether every glue is written, the detections included.

        The precondition of ``READY_FOR_REDACTION_REVIEW``
        (``review_states.final_volume_ready``, #263): review 2 judges
        the redactions of the corrected volume, so every output of the
        corrected volume must exist before the review opens.
        """
        return self.is_glued and bool(self.detections_key)


class PageRepairRequest(AbstractDateTimeModel):
    """One request for a page a person with the book must scan (#249).

    A reviewer finds a blurry page or a missing leaf. The reviewer has
    no book, so the finding is work for a scanner. This row keeps the
    finding until the scanner does the work. The finding used to go to
    a chat message, and the system kept nothing.

    A request is **not** a ``PageEdit``. A ``PageEdit`` is a decision
    about the document, and the apply (#206) builds it into the volume.
    A request is work for a person. It carries a free-text ``note``,
    and it has an end state a decision does not have: **fulfilled**.
    A reader of ``page_edits.open_edits`` never sees a request, so the
    apply cannot mistake one for a decision.

    What must not be broken:

    - **The address is a physical page of the original as uploaded**,
      1-based, the space ``PageEdit`` uses. ``pdf_page`` names the page
      to scan again (REPLACE). ``anchor_pdf_page`` names the page the
      missing leaf follows (INSERT), and 0 means "before page 1". The
      printed number is a label in ``logical_page`` and locates
      nothing.
    - **A request is dismissed, never deleted.** ``dismissed_at`` and
      ``dismissed_by`` close it. The row stays as the audit of what a
      reviewer asked for and who judged it unnecessary.
    - **Fulfilled is derived, not stamped.** A request is fulfilled
      when a standing ``INSERT_PAGE`` or ``REPLACE_PAGE`` edit exists
      at its address (``repairs._fulfilling_edits``), and made after
      the request. No writer stamps
      it, so the upload cannot race a stamp, and an undo of the upload
      (#232) reopens the request with no second writer.
    - **One open request per address.** The unique key is partial over
      the rows with no dismissal. A second request for the same page
      answers the first row. A dismissed row frees the address.
    - **``source_fingerprint`` is the scan's** at the time of the
      request. The original never changes (the apply writes another
      file), so it moves only when somebody re-uploads the volume. A
      request made against an earlier upload is shown with a mark,
      never dropped: it is for a person, and the person judges it.
    """

    class Action(models.TextChoices):
        """What the scanner must do."""

        INSERT = "insert", "Scan a missing page"
        REPLACE = "replace", "Scan this page again"

    scan = models.ForeignKey(
        Scan,
        on_delete=models.CASCADE,
        related_name="repair_requests",
    )
    action = models.CharField(
        max_length=16,
        choices=Action.choices,
        db_index=True,
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="repair_requests",
        help_text="Who found the page.",
    )
    pdf_page = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=(
            "REPLACE only: the 1-based page of the original PDF to scan again."
        ),
    )
    anchor_pdf_page = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=(
            "INSERT only: the 1-based original page the missing leaf "
            "follows. 0 puts it before page 1."
        ),
    )
    logical_page = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text=(
            "The printed page number, a label for the scanner. It "
            "never locates the page."
        ),
    )
    note = models.TextField(
        blank=True,
        default="",
        help_text="What the reviewer saw. Free text, cut at 500 characters.",
    )
    source_fingerprint = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=(
            "The scan's source fingerprint when the request was made. "
            "Blank matches anything."
        ),
    )
    dismissed_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text=(
            "When a person judged the request unnecessary. A stamped "
            "row is history, and it is never rewritten."
        ),
    )
    dismissed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="dismissed_repair_requests",
        help_text="Who dismissed the request. Null while it stands.",
    )

    class Meta:
        # The address order needs the column the action uses;
        # ``repairs.annotate_fulfilled`` orders by that.
        ordering = ["scan", "pk"]
        indexes = [
            models.Index(
                fields=["scan", "dismissed_at"],
                name="idx_repair_request_scan_open",
            ),
            models.Index(
                fields=["dismissed_at", "date_created"],
                name="idx_repair_request_queue",
            ),
        ]
        constraints = [
            # One address column per action, as on PageEdit: a null is
            # never a second meaning of a column.
            models.CheckConstraint(
                condition=(
                    models.Q(
                        action="insert",
                        pdf_page__isnull=True,
                        anchor_pdf_page__isnull=False,
                    )
                    | models.Q(
                        action="replace",
                        pdf_page__isnull=False,
                        anchor_pdf_page__isnull=True,
                    )
                ),
                name="repair_request_address_matches_action",
            ),
            # Partial over the open rows: a dismissed request frees the
            # address, so a later reviewer can ask again.
            # ``nulls_distinct=False`` because one of the two address
            # columns is always null.
            models.UniqueConstraint(
                fields=["scan", "action", "pdf_page", "anchor_pdf_page"],
                condition=models.Q(dismissed_at__isnull=True),
                nulls_distinct=False,
                name="uniq_open_repair_request_per_address",
            ),
        ]

    @property
    def address(self) -> int:
        """Return the page or the anchor this request names.

        :returns: ``pdf_page`` for a REPLACE, ``anchor_pdf_page`` for
            an INSERT.
        :rtype: int
        """
        if self.action == self.Action.INSERT:
            return self.anchor_pdf_page
        return self.pdf_page

    @property
    def is_stale(self) -> bool:
        """Return whether this request names an earlier upload of the scan.

        A person judges a stale request; nothing applies it, so it is
        marked and never dropped. Reads ``self.scan``, so a caller that
        lists many rows joins the scan first.

        :returns: Whether the fingerprints differ. A blank on either
            side matches anything, the rule of ``page_edits.is_stale``.
        :rtype: bool
        """
        mine, theirs = self.source_fingerprint, self.scan.source_fingerprint
        return bool(mine and theirs and mine != theirs)

    @property
    def nav_pdf_index(self) -> int:
        """Return the 0-based page the viewer scrolls to.

        A missing leaf has no page of its own, so the viewer shows the
        page before the gap. A gap before page 1 shows page 1.

        :returns: A 0-based PDF page index.
        :rtype: int
        """
        if self.action == self.Action.INSERT:
            return max(self.anchor_pdf_page - 1, 0)
        return self.pdf_page - 1

    def __str__(self):
        where = (
            f"after p.{self.anchor_pdf_page}"
            if self.action == self.Action.INSERT
            else f"p.{self.pdf_page}"
        )
        state = " [dismissed]" if self.dismissed_at is not None else ""
        return f"{self.get_action_display()} {where}{state}"


class PendingUpload(AbstractDateTimeModel):
    """Tracks an authorized-but-unconfirmed direct-to-S3 upload.

    Created when the browser requests a presigned POST for a scan's
    original PDF (see ``presign_scan_upload``). The browser uploads the
    bytes straight to S3, then calls ``confirm_scan_upload`` which
    verifies the object landed, attaches it to the scan, and deletes
    this row. Rows that are never confirmed (the user closed the tab
    mid-upload) are swept — along with their fileless scans — by the
    ``cleanup_processing_tmp`` daemon task.

    :ivar id: UUID primary key, also handed to the browser so
        ``confirm_scan_upload`` can look the row up.
    :ivar scan: The scan the upload belongs to. Deleted with the scan.
    :ivar s3_key: The full S3 key the presigned POST targets (the scan's
        processing prefix + original filename).
    :ivar expected_size: Size in bytes the browser reported at presign
        time, recorded for diagnostics/auditing (also shown in admin).
        The size ceiling is enforced separately: the presign view rejects
        anything over ``MAX_ORIGINAL_UPLOAD_SIZE`` and the presigned POST
        ``content-length-range`` condition caps the object at that limit.
    :ivar content_type: MIME type the browser reported.
    :ivar action: The post-upload action the uploader chose
        (``upload_only`` or ``upload_validate``), stored so recovery can
        replay the original intent if ``confirm_scan_upload`` never ran.
    :ivar created_by: The user who initiated the upload.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scan = models.ForeignKey(
        Scan,
        on_delete=models.CASCADE,
        related_name="pending_uploads",
    )
    s3_key = models.CharField(max_length=1024)
    expected_size = models.PositiveBigIntegerField()
    content_type = models.CharField(max_length=100, blank=True)
    action = models.CharField(
        max_length=32,
        choices=UploadAction.choices,
        default=UploadAction.UPLOAD_ONLY,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
    )

    def __str__(self):
        return f"pending upload for scan {self.scan_id} ({self.s3_key})"


# ── External compute: one row per submitted job ───────────────────────


class JobProvider(models.TextChoices):
    """Who runs a job: how it is submitted and how progress is polled.

    Separate from :class:`JobEngine` because one provider serves
    several engines. RunPod hands back a job id and answers ``GET
    /status``; Mistral and doctor answer on their own endpoints in
    their own shapes. The split keeps polling written once per
    provider, and makes moving an engine elsewhere a value change.
    """

    RUNPOD = "runpod", "RunPod Serverless"
    MISTRAL = "mistral", "Mistral API"
    DOCTOR = "doctor", "Doctor"


class JobEngine(models.TextChoices):
    """What a job actually does: the model or program that runs.

    ``BLACKLETTER`` runs YOLO detection over a volume; the OCR engines
    read opinion PDFs. ``BITONAL`` is not a model but the 1-bit
    conversion pass, which gets rows because it runs on doctor rather
    than on the portal host (#158).

    The engine names the library, not the checkpoint it loads: the
    weights are a worker input (``yolo.MODELS``), so a later checkpoint
    is a payload change rather than a new engine. Its PaddleOCR half
    went with the legacy pipeline (#173), which is what the label says.
    """

    BLACKLETTER = "blackletter", "blackletter (YOLO detection)"
    BITONAL = "bitonal", "Bitonal conversion"
    DOTS_MOCR = "dots_mocr", "dots.mocr"
    MISTRAL_OCR = "mistral_ocr", "Mistral OCR"
    SURYA = "surya", "Surya"
    LIGHTON_OCR = "lighton_ocr", "LightOnOCR"


class JobStage(models.TextChoices):
    """Which pipeline step a job belongs to.

    A stage is a barrier, not a synonym for an engine: ``EXTRACT``
    holds the several engines reading the same document at once, and
    the next step starts when every job in the stage is CONSUMED.
    ``engine`` is therefore part of the unique key, or two engines
    would collide on one target.

    Stages come in two shapes, which is what ``opinion`` on the job
    expresses. ``CONVERT``, ``DETECT`` and ``ANALYZE`` run once over
    the volume, before review. ``EXTRACT`` and ``TIEBREAK`` run after
    file generation, once per opinion PDF, so 300 opinions read by
    three engines is 900 rows.

    Two things the daemon has to respect. Local steps own no rows and
    sit between stages: reconciling two engines' text, pairing, rect
    computation, and the file generation that produces the opinion
    PDFs. And an empty stage is satisfied by zero rows, since engines
    that agree on an opinion produce no ``TIEBREAK`` job for it.

    Declared in run order, but nothing reads that order. Local steps
    and empty stages make the sequence more than an enum walk, so what
    runs next will be an explicit pipeline definition.
    """

    CONVERT = "convert", "Convert to bitonal"
    DETECT = "detect", "Detect (YOLO)"
    ANALYZE = "analyze", "Analyze (page numbers)"
    EXTRACT = "extract", "Extract text"
    TIEBREAK = "tiebreak", "Tiebreak disputed reads"


#: Stages whose unit of work is one opinion PDF rather than the volume.
#: A tuple, not a frozenset: it is embedded in a database constraint,
#: and an unordered container rewrites the migration every time the
#: interpreter hashes it differently.
OPINION_LEVEL_STAGES = (JobStage.EXTRACT, JobStage.TIEBREAK)


class JobStatus(models.TextChoices):
    """Normalized job lifecycle; provider states are mapped onto it.

    ``COMPLETED`` means the provider reports the work is done;
    ``CONSUMED`` means we have read the result and applied it. Only a
    CONSUMED job is safe from a provider's result purge. Mirrors the
    SUCCEEDED / FINISHED split in ``ai.LLMTaskStatusChoices``.

    ``EXPIRED`` is data loss rather than failure: the provider
    finished, and neither its status call nor the result object says
    what it produced. Separate from FAILED so it reads as a bug in our
    polling rather than a bad job.
    """

    PENDING = "pending", "Pending submit"
    SUBMITTED = "submitted", "Submitted"
    IN_QUEUE = "in_queue", "In queue"
    IN_PROGRESS = "in_progress", "In progress"
    COMPLETED = "completed", "Completed (result not yet applied)"
    CONSUMED = "consumed", "Result applied"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"
    EXPIRED = "expired", "Expired (result lost)"


#: Jobs the provider is still working on, so worth polling.
IN_FLIGHT_JOB_STATUSES = frozenset(
    {
        JobStatus.SUBMITTED,
        JobStatus.IN_QUEUE,
        JobStatus.IN_PROGRESS,
    }
)

#: Jobs the daemon still has work to do on. COMPLETED belongs here and
#: not with the terminal states: the provider is finished but we have
#: not applied the result, and calling that done loses the output.
OPEN_JOB_STATUSES = frozenset(
    {JobStatus.PENDING, JobStatus.COMPLETED} | IN_FLIGHT_JOB_STATUSES
)

#: Jobs that ended without their work being applied. Separate from the
#: terminal set below because a run holding one can never finish -- it
#: will not complete and cannot be merged -- so it has to be replaced
#: by a fresh run rather than picked back up.
DEAD_JOB_STATUSES = frozenset(
    {
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.EXPIRED,
    }
)

#: Jobs nothing will happen to again without an explicit retry.
TERMINAL_JOB_STATUSES = frozenset({JobStatus.CONSUMED}) | DEAD_JOB_STATUSES


class ExternalJobQuerySet(AutoNowQuerySet):
    """Queries the daemon runs to decide what to do next.

    Subclasses :class:`AutoNowQuerySet` so bulk writes through these
    still stamp ``date_modified``.
    """

    def open(self):
        """Return jobs the daemon still has work to do on.

        :returns: Jobs awaiting submit, in flight, or completed but not
            yet applied.
        :rtype: ExternalJobQuerySet
        """
        return self.filter(status__in=OPEN_JOB_STATUSES)

    def in_flight(self):
        """Return jobs the provider is still working on.

        :returns: Jobs worth polling for a status change.
        :rtype: ExternalJobQuerySet
        """
        return self.filter(status__in=IN_FLIGHT_JOB_STATUSES)

    def terminal(self):
        """Return jobs that will not change without an explicit retry.

        :returns: Consumed, failed, cancelled, and expired jobs.
        :rtype: ExternalJobQuerySet
        """
        return self.filter(status__in=TERMINAL_JOB_STATUSES)

    def overdue(self, now=None):
        """Return in-flight jobs whose deadline has passed.

        Per job rather than per scan: once a stage fans out, one wedged
        shard has to be cancellable and resubmittable without touching
        its siblings.

        :param now: Comparison time; defaults to ``timezone.now()``.
        :returns: In-flight jobs past their deadline.
        :rtype: ExternalJobQuerySet
        """
        if now is None:
            now = timezone.now()
        return self.in_flight().filter(
            deadline__isnull=False, deadline__lt=now
        )


class ExternalJob(AbstractDateTimeModel):
    """One unit of work handed to an external compute provider.

    Supersedes tracking a single provider job on ``Scan``: a scan has
    many jobs, and a fanned-out stage has several in flight at once
    across providers. The daemon keeps no in-memory job state and
    recomputes what to submit, poll, harvest, or cancel from these rows
    every tick, which is what makes an interrupted daemon resumable.
    Nothing about "what runs next" may live in a Python call stack.

    Retries mutate the row rather than inserting one per attempt, the
    opposite of the ``ai.LLMTask`` idiom, because the daemon asks
    "latest state per shard" every tick and a stable unique key keeps
    that a plain filter. The cost: the row is the only copy of its own
    state, so a resubmission must bump ``attempt`` (which re-addresses
    the result object) and call :meth:`push_attempt` (which preserves
    the previous provider handle) before overwriting anything.

    Only work that leaves the process gets a row. The local steps
    between stages own none.
    """

    scan = models.ForeignKey(
        Scan,
        on_delete=models.CASCADE,
        related_name="jobs",
        help_text=(
            "The volume this work belongs to, set even when the target "
            "is a single opinion. Denormalized so 'has this scan "
            "anything in flight', the daemon's most frequent question, "
            "needs no join, and so the cascade is rooted at the scan."
        ),
    )
    opinion = models.ForeignKey(
        OpinionScan,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="jobs",
        help_text=(
            "The opinion PDF this job read, for the post-generation "
            "stages. Null for the volume-level stages, which run before "
            "any opinion exists. Must belong to ``scan``.\n\n"
            "CASCADE rather than SET_NULL: an orphaned extract row "
            "would break the invariant that an opinion-level stage has "
            "an opinion, and would leave the stage barrier counting a "
            "row with no target. The consequence is that "
            "``run_generate_files`` deletes and recreates a scan's "
            "OpinionScan rows, so regenerating files discards every "
            "extraction job with them. Right when the opinion changed, "
            "wasteful when it did not, which is why preserving "
            "unchanged opinion rows (issue #165) has to land before we "
            "pay for hundreds of jobs a volume."
        ),
    )
    apply_run = models.ForeignKey(
        ApplyRun,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="jobs",
        help_text=(
            "The apply run this row works for (issue #224): a one-page "
            "shard of a page a curator added or changed. Null for the "
            "volume runs. Every reader of 'the live volume run' filters "
            "these rows out, and the apply reads its rows through this "
            "key and never by run number."
        ),
    )
    stage = models.CharField(
        max_length=20,
        choices=JobStage.choices,
    )
    engine = models.CharField(
        max_length=32,
        choices=JobEngine.choices,
    )
    provider = models.CharField(
        max_length=20,
        choices=JobProvider.choices,
    )
    status = models.CharField(
        max_length=20,
        choices=JobStatus.choices,
        default=JobStatus.PENDING,
    )
    run = models.PositiveSmallIntegerField(
        default=1,
        help_text=(
            "Stage-run generation. Re-running a stage (a re-validate, a "
            "reprocess) creates rows at the next run number and keeps "
            "the previous run as history; retrying a single shard "
            "mutates its row instead. Scoped per engine, not per stage: "
            "an engine's live jobs are its rows at its own max(run), "
            "and a stage's are the union of those, so re-running one "
            "engine of a multi-engine stage cannot hide another "
            "engine's rows from the barrier."
        ),
    )
    attempt = models.PositiveSmallIntegerField(
        default=1,
        help_text=(
            "Which submission of this row is in flight, incremented on "
            "every resubmission whatever the reason, and carried in "
            "``result_key`` so two attempts of one shard never write to "
            "the same object. Not derived from retry_count, which "
            "accounts for why a job was resubmitted and can stay flat "
            "across one (a daemon restart). A key reused across "
            "attempts lets an abandoned worker's late upload be "
            "harvested as the current attempt's output."
        ),
    )
    external_id = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text=(
            "The provider's job id, set on submit and kept after the "
            "terminal state for auditing and billing lookups. Lets a "
            "restarted daemon reattach to a job that is still running "
            "instead of paying for it twice."
        ),
    )

    # ── what this job covers ─────────────────────────────────────────
    shard_index = models.PositiveIntegerField(
        default=0,
        help_text=(
            "Position of this job in its target's split, counting from "
            "zero, and part of the unique key. Volume passes are split "
            "into page ranges that run at once to finish faster "
            "(bitonal, dots.mocr). An opinion PDF is read whole, so it "
            "stays 0."
        ),
    )
    shard_count = models.PositiveIntegerField(
        default=1,
        help_text="How many jobs the target was split into; 1 if read whole.",
    )
    input_key = models.CharField(
        max_length=1024,
        blank=True,
        default="",
        help_text=(
            "S3 key of the exact bytes sent: an opinion PDF for the "
            "post-generation stages, the volume or one of its shards "
            "for the earlier ones. Recorded because opinion PDFs are "
            "regenerated and renamed when pairing changes, so the FK "
            "alone does not say which file version was read."
        ),
    )
    input_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=(
            "Hash of the document at ``input_key`` when it was sent. "
            "Answers what the FK cannot: whether an extraction still "
            "describes the current file, or the opinion has been "
            "regenerated underneath it and has to be read again."
        ),
    )
    input_manifest = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Provider-shaped description of the work, for jobs an "
            "input key does not fully describe. A tiebreak read is a "
            "list of regions rather than a whole document:\n\n"
            '{"crops": [{"key": "page_0007_84_132_1620_230", '
            '"page_index": 7, "bbox": [84, 132, 1620, 230]}]}\n\n'
            "and any job may carry per-job tuning overrides, such as "
            '{"dpi": 400}.'
        ),
    )
    source_fingerprint = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=(
            "The shard set this row was cut for: '{size_bytes}:"
            "{page_count}' of the original, the value "
            "sharding.ensure_shards stamps on Scan.source_fingerprint. "
            "Stamped by jobs.ensure_shard_jobs on every row of every "
            "stage, so 'has this set been detected' is one query "
            "(#250). Not part of the shard identity in input_manifest. "
            "Blank on a row written before the column, which matches "
            "anything."
        ),
    )

    # ── result ───────────────────────────────────────────────────────
    result_key = models.CharField(
        max_length=1024,
        blank=True,
        default="",
        help_text=(
            "S3 key the worker uploads its output to, assigned by us at "
            "submit time and handed over as a presigned PUT. Scoped to "
            "run, shard, and attempt, and never overwritten: that makes "
            "a lost status response recoverable with a head_object, and "
            "stops the probe harvesting another run's or another "
            "attempt's output."
        ),
    )

    # ── lifecycle ────────────────────────────────────────────────────
    submitted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When the job was handed to the provider. Also the "
            "reference point for deciding whether an object at "
            "``result_key`` belongs to this attempt."
        ),
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    last_polled_at = models.DateTimeField(null=True, blank=True)
    deadline = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Wall-clock ceiling stamped at submit: a base timeout plus "
            "an allowance for this job's pages. Per job rather than per "
            "scan so one wedged shard is cancelled and resubmitted "
            "without stalling its siblings."
        ),
    )

    # ── failure accounting ───────────────────────────────────────────
    retry_count = models.PositiveSmallIntegerField(
        default=0,
        help_text="Transient failures retried for this job.",
    )
    error_code = models.CharField(max_length=64, blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    provider_meta = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Diagnostic rather than structural: which endpoint served "
            "it, worker metadata, timings, sizes, cost, and an "
            "append-only ``attempts`` list of prior attempts so "
            "mutating this row on retry still keeps its history."
        ),
    )

    objects = ExternalJobQuerySet.as_manager()

    class Meta:
        # By id, not by the relation: ordering on ``scan``/``opinion``
        # inherits their own Meta.ordering and joins both tables into
        # every query of this one.
        ordering = [
            "scan_id",
            "stage",
            "run",
            "engine",
            "opinion_id",
            "shard_index",
        ]
        constraints = [
            # Two uniqueness rules, because the two stage shapes have
            # different targets. Conditional rather than one constraint
            # over both columns: Postgres treats NULLs as distinct, so
            # a single key including ``opinion`` would place no limit
            # at all on volume-level rows.
            models.UniqueConstraint(
                fields=["scan", "stage", "engine", "run", "shard_index"],
                condition=models.Q(opinion__isnull=True),
                name="unique_volume_job_per_engine_run",
            ),
            models.UniqueConstraint(
                fields=["opinion", "stage", "engine", "run", "shard_index"],
                condition=models.Q(opinion__isnull=False),
                name="unique_opinion_job_per_engine_run",
            ),
            # A stage's shape decides whether an opinion is required.
            # An extract row without one attributes a single opinion's
            # work to the whole volume and collides with its siblings;
            # a detect row with one claims a target that did not exist
            # when it ran.
            models.CheckConstraint(
                condition=(
                    models.Q(
                        stage__in=OPINION_LEVEL_STAGES, opinion__isnull=False
                    )
                    | (
                        ~models.Q(stage__in=OPINION_LEVEL_STAGES)
                        & models.Q(opinion__isnull=True)
                    )
                ),
                name="job_opinion_matches_stage",
            ),
            models.CheckConstraint(
                condition=models.Q(shard_index__lt=models.F("shard_count")),
                name="job_shard_index_within_count",
            ),
            models.CheckConstraint(
                condition=models.Q(run__gte=1),
                name="job_run_positive",
            ),
        ]
        indexes = [
            # The daemon's hot query: everything still open, and which
            # of those is past its deadline.
            models.Index(
                fields=["status", "deadline"],
                name="idx_job_status_deadline",
            ),
            models.Index(
                fields=["scan", "stage", "run"],
                name="idx_job_scan_stage_run",
            ),
            models.Index(
                fields=["scan", "status"],
                name="idx_job_scan_status",
            ),
            # "What is left to do for this opinion", the per-opinion view
            # of a stage that can hold hundreds of them.
            models.Index(
                fields=["opinion", "stage", "status"],
                name="idx_job_opinion_stage",
            ),
            models.Index(fields=["external_id"], name="idx_job_external_id"),
        ]

    @classmethod
    def next_run(cls, scan, stage, engine, opinion=None):
        """Return the run number a fresh submission should use.

        Re-running takes the next run rather than reusing the current
        one, keeping the previous run's rows and result objects
        addressable instead of overwritten.

        Scoped per engine because a stage holds several. Per-stage
        scoping would land two engines submitted one after another on
        different runs (the second call sees the first's row), and
        would let a single-engine re-run raise max(run) so the barrier
        stopped seeing the other engine's live rows.

        Scoped per opinion for the same reason one level down:
        re-reading one opinion of three hundred must not renumber the
        other 299, which is what makes "re-extract just this opinion"
        a supported operation rather than a whole-volume rerun.

        :param scan: The Scan, or its pk.
        :param stage: A :class:`JobStage` value.
        :param engine: A :class:`JobEngine` value.
        :param opinion: The OpinionScan (or its pk) for an
            opinion-level stage; omit for the volume-level stages.
        :returns: ``max(run) + 1`` for that target, or 1 if it has
            never run.
        :rtype: int
        """
        current = cls.objects.filter(
            scan=scan, stage=stage, engine=engine, opinion=opinion
        ).aggregate(models.Max("run"))["run__max"]
        return (current or 0) + 1

    def clean(self):
        """Validate that an opinion-level job targets its own scan's opinion.

        The database can enforce that an opinion is present or absent
        for the stage, but not that it belongs to the right volume, so
        this covers the hand-edit path.

        :raises ValidationError: If ``opinion`` belongs to another scan.
        """
        super().clean()
        if (
            self.opinion_id
            and self.scan_id
            and self.opinion.scan_id != self.scan_id
        ):
            raise ValidationError(
                {
                    "opinion": (
                        "Opinion belongs to scan "
                        f"{self.opinion.scan_id}, not {self.scan_id}."
                    )
                }
            )

    @property
    def is_open(self):
        """Whether the daemon still has work to do on this job.

        :returns: True while the job is pending, in flight, or completed
            but not yet applied.
        :rtype: bool
        """
        return self.status in OPEN_JOB_STATUSES

    @property
    def is_terminal(self):
        """Whether this job is finished for good, absent an explicit retry.

        :returns: True for consumed, failed, cancelled, and expired.
        :rtype: bool
        """
        return self.status in TERMINAL_JOB_STATUSES

    def is_overdue(self, now=None):
        """Whether an in-flight job has run past its deadline.

        :param now: Comparison time; defaults to ``timezone.now()``.
        :returns: True if the job is in flight and past its deadline.
        :rtype: bool
        """
        if self.deadline is None or self.status not in IN_FLIGHT_JOB_STATUSES:
            return False
        return self.deadline < (now or timezone.now())

    def push_attempt(self, save=True):
        """Record the current attempt in ``provider_meta["attempts"]``.

        Called before a retry mutates the row. Since a retry reuses the
        row (see the class docstring), this list is the only place an
        earlier provider id, failure, or result key survives.

        :param save: Whether to persist ``provider_meta`` immediately.
        :returns: The attempts list after appending.
        :rtype: list
        """
        if not isinstance(self.provider_meta, dict):
            self.provider_meta = {}
        attempts = self.provider_meta.setdefault("attempts", [])
        attempts.append(
            {
                "attempt": self.attempt,
                "external_id": self.external_id,
                "status": self.status,
                "error_code": self.error_code,
                "error_message": self.error_message,
                "result_key": self.result_key,
                "submitted_at": (
                    self.submitted_at.isoformat()
                    if self.submitted_at
                    else None
                ),
                "completed_at": (
                    self.completed_at.isoformat()
                    if self.completed_at
                    else None
                ),
            }
        )
        if save:
            self.save(update_fields=["provider_meta"])
        return attempts

    def __str__(self):
        target = (
            f"opinion {self.opinion_id}"
            if self.opinion_id
            else f"scan {self.scan_id}"
        )
        shard = (
            ""
            if self.shard_count == 1
            else f" shard {self.shard_index + 1}/{self.shard_count}"
        )
        return (
            f"{target} {self.stage}/{self.engine}{shard} "
            f"({self.get_status_display()})"
        )
