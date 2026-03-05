from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.deconstruct import deconstructible


class Status(models.TextChoices):
    UPLOADED = "uploaded", "Uploaded"
    PROCESSING = "processing", "Processing"
    PENDING_REVIEW = "pending_review", "Pending Review"
    APPROVED = "approved", "Approved"
    EXTRACTED = "extracted", "Extracted"


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

    class Meta:
        abstract = True


class Reporter(AbstractDateTimeModel):
    """A legal reporter series (e.g. U.S. Reports, Federal Reporter)."""

    short_name = models.CharField(max_length=20, unique=True, db_index=True)
    full_name = models.CharField(max_length=100)

    class Meta:
        ordering = ["full_name"]

    def __str__(self):
        return self.full_name


def book_upload_path(instance, filename):
    """Generate upload path for book scan PDFs.

    Example: ``books/f3d/original/42_f3d_1-200_full.pdf``

    :param instance: The Scan model instance.
    :type instance: Scan
    :param filename: The original filename.
    :type filename: str
    :returns: The upload path.
    :rtype: str
    """
    return (
        f"books/{instance.reporter.short_name}/original/"
        f"{instance.volume}_{instance.reporter.short_name}"
        f"_{instance.start_page}-{instance.end_page}"
        f"_{instance.source}.pdf"
    )


def compressed_upload_path(instance, filename):
    """Generate upload path for compressed book scan PDFs.

    Example: ``books/f3d/compressed/42_f3d_1-200_full.pdf``

    :param instance: The Scan model instance.
    :type instance: Scan
    :param filename: The original filename.
    :type filename: str
    :returns: The upload path.
    :rtype: str
    """
    return (
        f"books/{instance.reporter.short_name}/compressed/"
        f"{instance.volume}_{instance.reporter.short_name}"
        f"_{instance.start_page}-{instance.end_page}"
        f"_{instance.source}.pdf"
    )


def book_cover_path(instance, filename):
    """Generate upload path for book cover images.

    Example: ``books/f3d/42_f3d_cover.jpg``

    :param instance: The Scan model instance.
    :type instance: Scan
    :param filename: The original filename.
    :type filename: str
    :returns: The upload path.
    :rtype: str
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

    :param subfolder: Subdirectory name (e.g. "unredacted", "masked", "redacted").
    :type subfolder: str
    """

    def __init__(self, subfolder):
        self.subfolder = subfolder

    def __call__(self, instance, filename):
        """Generate the upload path for the given instance and filename.

        :param instance: The OpinionScan model instance.
        :type instance: OpinionScan
        :param filename: The original filename.
        :type filename: str
        :returns: The upload path.
        :rtype: str
        """
        ext = filename.rsplit(".", 1)[-1] if "." in filename else "pdf"
        return (
            f"opinions/{instance.reporter.short_name}"
            f"/{instance.volume}/{self.subfolder}"
            f"/{instance.reporter.short_name}.{instance.volume}"
            f".{instance.page_start:04d}-{instance.page_end:04d}.{ext}"
        )


class ScanManager(models.Manager):
    """Custom manager for the Scan model."""

    def needs_processing(self):
        """Return scans that are uploaded but missing redacted or compressed PDFs.

        :returns: QuerySet of scans needing processing.
        :rtype: QuerySet
        """
        return self.filter(
            status=Status.UPLOADED,
        ).filter(Q(redacted_pdf="") | Q(compressed_pdf=""))


class Scan(AbstractDateTimeModel):
    objects = ScanManager()

    reporter = models.ForeignKey(
        Reporter,
        on_delete=models.PROTECT,
        related_name="scans",
    )
    volume = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    number_of_pages = models.PositiveIntegerField(
        validators=[MinValueValidator(1)]
    )
    start_page = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    end_page = models.PositiveIntegerField(validators=[MinValueValidator(1)])
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
    )
    redacted_pdf = models.FileField(
        null=True,
        blank=True,
    )
    compressed_pdf = models.FileField(
        upload_to=compressed_upload_path,
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
    notes = models.TextField(blank=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["reporter", "volume"],
                name="idx_reporter_volume",
            ),
            models.Index(fields=["status"], name="idx_status"),
            models.Index(fields=["uploaded_by"], name="idx_uploaded_by"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["reporter", "volume", "source"],
                name="unique_reporter_volume_source",
                violation_error_message=(
                    "A scan for this reporter, volume, and source"
                    " already exists."
                ),
            ),
        ]
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

    @property
    def is_stale_processing(self):
        """Check if this scan has been stuck in PROCESSING too long.

        :returns: True if the scan has exceeded the processing timeout.
        :rtype: bool
        """
        if self.status != Status.PROCESSING or not self.processed_at:
            return False
        timeout = settings.DAEMON_PROCESSING_TIMEOUT
        return (
            timezone.now() - self.processed_at
        ).total_seconds() > timeout

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
    original_pdf = models.FileField(upload_to=opinion_pdf_path("unredacted"))
    masked_pdf = models.FileField(
        upload_to=opinion_pdf_path("masked"),
        null=True,
        blank=True,
    )
    redacted_pdf = models.FileField(
        upload_to=opinion_pdf_path("redacted"),
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

    class Meta:
        indexes = [
            models.Index(fields=["scan"], name="idx_opinion_scan"),
            models.Index(
                fields=["reporter", "volume"],
                name="idx_opinion_reporter_volume",
            ),
            models.Index(fields=["status"], name="idx_opinion_status"),
        ]
        ordering = ["-date_created"]

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
