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
    opinions_json = models.JSONField(
        default=list,
        blank=True,
        help_text="Opinion boundary data.",
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
    margin_rects = models.JSONField(
        default=list,
        blank=True,
        help_text="Per-page margin rects in PDF points.",
    )
    redaction_rects = models.JSONField(
        default=list,
        blank=True,
        help_text="Per-page redaction rects in image pixels.",
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


class Detection(AbstractDateTimeModel):
    """YOLO detection stored in DB.

    Each row is one bounding box from one model.
    Coordinates are in image pixels.
    """

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

    class Meta:
        ordering = ["page_index", "y0", "x0"]
        indexes = [
            models.Index(
                fields=["scan", "page_index"],
                name="idx_det_scan_page",
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
    - **A decision is closed, never rewritten and never deleted.** The
      apply (#206) stamps ``applied_at``; a curator who takes the
      decision back stamps ``withdrawn_at`` and ``withdrawn_by``
      (#232). Either stamp makes the row history. So every unique
      constraint is partial over the rows that carry neither: a
      curator who edits the same page again writes a new row, against
      the fingerprint of the original as it is then. A second file
      uploaded for one page withdraws the first row rather than
      writing over it -- the audit must show every file a person
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
            "When the apply (#206) built this decision into a new "
            "original. A stamped row is history: it is never rewritten."
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
            # The unique keys are partial over the open rows. A row
            # leaves that set in one of two ways, and both are
            # history: the apply built it in, or the curator took it
            # back (#232). After either, the same page may be edited
            # again.
            models.UniqueConstraint(
                fields=["scan", "kind", "pdf_page"],
                condition=(
                    models.Q(applied_at__isnull=True)
                    & models.Q(withdrawn_at__isnull=True)
                    & models.Q(pdf_page__isnull=False)
                    & ~models.Q(kind="dismiss_issue")
                ),
                name="uniq_open_page_edit_per_page",
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
                    models.Q(applied_at__isnull=True)
                    & models.Q(withdrawn_at__isnull=True)
                    & models.Q(kind="dismiss_issue")
                ),
                nulls_distinct=False,
                name="uniq_open_dismissal_per_check",
            ),
            models.UniqueConstraint(
                fields=["scan", "anchor_pdf_page", "ordinal"],
                condition=(
                    models.Q(applied_at__isnull=True)
                    & models.Q(withdrawn_at__isnull=True)
                    & models.Q(kind="insert_page")
                ),
                name="uniq_open_insert_per_gap",
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
        if self.applied_at is not None:
            state = " [applied]"
        elif self.withdrawn_at is not None:
            state = " [withdrawn]"
        return f"{self.get_kind_display()} {where}{value}{state}"


class ExtractionStatus(models.TextChoices):
    """Lifecycle of a Page through the LLM extraction pipeline."""

    PENDING = "pending", "Pending"
    SUBMITTED = "submitted", "Submitted to batch"
    EXTRACTED = "extracted", "Extracted"
    FAILED = "failed", "Failed"
    PROMPT_BLOCKED = "prompt_blocked", "Blocked at prompt level"
    RESPONSE_BLOCKED = "response_blocked", "Blocked at response level"
    RECITATION_BLOCKED = (
        "recitation_blocked",
        "Blocked by recitation filter",
    )


class ExtractedBy(models.TextChoices):
    """Which pipeline produced the page's XML content."""

    LLM = "LLM", "LLM"
    OCR_FALLBACK = "ocr-fallback", "Local OCR fallback"
    HUMAN = "human", "Human review"
    BLANK_AUTO = "blank-auto", "Blank page (auto)"


class Page(AbstractDateTimeModel):
    """One PDF page of a ``Scan`` produced by ``run_generate_files``.

    Holds the per-page artifact path plus all extraction state. The
    user prompt that drives extraction lives in ``ai.Prompt``; this
    model FKs to whichever Prompt row is currently bound to the page.
    Tweaking the prompt creates a new Prompt and repoints this FK.

    XML extraction details (``xml_content``, ``status``, etc.) are
    populated by the Phase 2 batch flow.
    """

    scan = models.ForeignKey(
        Scan,
        on_delete=models.CASCADE,
        related_name="pages",
    )
    page_index = models.PositiveIntegerField(
        help_text=(
            "0-based, matching ``Detection.page_index``. The PDF "
            "filename is ``llm/page_{page_index + 1:04d}.pdf``."
        ),
    )
    book_page = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text=(
            "Visible page number printed on this page of the book. "
            "Usually one integer like '687'; rarely a range like "
            "'678-686' when a single PDF page collapses several book "
            "pages."
        ),
    )

    pdf_path = models.CharField(
        max_length=512,
        blank=True,
        default="",
        help_text=(
            "Relative to ``scan.output_dir``, e.g. ``llm/page_0001.pdf``."
        ),
    )
    user_prompt = models.ForeignKey(
        "ai.Prompt",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="pages",
        help_text=(
            "Current user prompt for this page. A tweaked prompt is a "
            "new Prompt row; this FK is repointed and the prior Prompt "
            "stays as history."
        ),
    )
    xml_content = models.TextField(
        blank=True,
        default="",
        help_text=("Extracted XML for this page."),
    )
    status = models.CharField(
        max_length=32,
        choices=ExtractionStatus.choices,
        default=ExtractionStatus.PENDING,
    )
    extracted_by = models.CharField(
        max_length=32,
        choices=ExtractedBy.choices,
        blank=True,
        default="",
    )
    needs_review = models.BooleanField(
        default=False,
        help_text=(
            "Set when the OCR fallback ran or the model output looked "
            "suspicious. Triage view filters by this."
        ),
    )

    # Per-attempt LLM state lives on ``ai.LLMTask`` (GenericFK back to
    # this Page). Page only carries the page-level rollup: which row
    # is the canonical extraction (xml_content above), how the work
    # was sourced (``extracted_by``), and overall lifecycle state
    # (``status``). Cost, tokens, retry-level errors, and provider
    # specifics are LLMTask/LLMRequest concerns.

    expected_opinion_starts = models.PositiveIntegerField(default=0)
    expected_opinion_ends = models.PositiveIntegerField(default=0)

    detections = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "All YOLO detections on this page, filtered from "
            "``detections.json`` at sync time."
        ),
    )
    is_blank = models.BooleanField(
        default=False,
        help_text=("Body is entirely covered by headnote redactions."),
    )

    class Meta:
        ordering = ("scan", "page_index")
        constraints = [
            models.UniqueConstraint(
                fields=["scan", "page_index"],
                name="unique_page_scan_index",
            ),
        ]
        indexes = [
            models.Index(
                fields=["scan", "page_index"],
                name="idx_page_scan_idx",
            ),
            models.Index(fields=["status"], name="idx_page_status"),
            models.Index(
                fields=["scan", "needs_review"],
                name="idx_page_review",
            ),
        ]

    def __str__(self):
        return f"scan {self.scan_id} p{self.page_index:04d}"


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
