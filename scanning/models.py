from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.deconstruct import deconstructible

from scanning.storage import LocalProcessingStorage

_local_storage = LocalProcessingStorage()


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
    UPLOADED = "uploaded", "Uploaded"
    QUEUED = "queued", "Queued"
    PROCESSING = "processing", "Processing"
    PENDING_REVIEW = "pending_review", "Pending Review"
    APPROVED = "approved", "Approved"
    EXTRACTED = "extracted", "Extracted"
    ERROR = "error", "Error"
    ERROR_MAX_RETRIES = "error_max_retries", "Error (retry cap hit)"
    CANCELLED = "cancelled", "Cancelled"


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
        max_length=20,
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

        1. ``output_dir/<name>.original.pdf`` (present after upload or
           after pulling from S3).
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
    masked_pdf = models.FileField(
        upload_to=opinion_pdf_path("masked"),
        storage=_local_storage,
        max_length=512,
        null=True,
        blank=True,
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

    # User actions (from scanning views)
    PROCESS_FLAG = "process_flag", "User-flagged issue"
    SUPPRESS_DETECTION = "suppress_detection", "Suppress a detection"
    ADD_DETECTION = "add_detection", "Add a detection"
    APPROVE_DETECTION = "approve_detection", "Approve a detection"


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
        """YOLO model tier or source of the detection."""

        SMALL = "small", "Small"
        MEDIUM = "medium", "Medium"
        LARGE = "large", "Large"
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


class PageInsert(AbstractDateTimeModel):
    """User-uploaded image to replace a missing page."""

    scan = models.ForeignKey(
        Scan,
        on_delete=models.CASCADE,
        related_name="inserts",
    )
    logical_page_number = models.PositiveIntegerField()
    image = models.FileField(upload_to="page_inserts/", storage=_local_storage)

    class Meta:
        unique_together = ("scan", "logical_page_number")

    def __str__(self):
        return f"Insert p.{self.logical_page_number} for {self.scan}"


class PageDeletion(AbstractDateTimeModel):
    """Page marked for deletion from the PDF."""

    scan = models.ForeignKey(
        Scan,
        on_delete=models.CASCADE,
        related_name="deletions",
    )
    pdf_page = models.PositiveIntegerField(
        help_text="1-based PDF page number.",
    )

    class Meta:
        unique_together = ("scan", "pdf_page")

    def __str__(self):
        return f"Delete PDF p.{self.pdf_page} from {self.scan}"
