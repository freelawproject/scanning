from django.conf import settings
from django.core.validators import FileExtensionValidator
from django.db import models
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
    NO_STATUS = "no_status", "No status"
    OK = "ok", "OK"
    GAP = "gap", "Gap"
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


class Scan(AbstractDateTimeModel):
    reporter = models.ForeignKey(
        Reporter,
        on_delete=models.PROTECT,
        related_name="scans",
    )
    volume = models.PositiveIntegerField()
    number_of_pages = models.PositiveIntegerField()
    start_page = models.PositiveIntegerField()
    end_page = models.PositiveIntegerField()
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
        ordering = ["-date_created"]

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
    volume = models.PositiveIntegerField()
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
    page_start = models.PositiveIntegerField()
    page_end = models.PositiveIntegerField()
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

    def __str__(self):
        return (
            f"{self.reporter} vol. {self.volume} opinion"
            f" ({self.get_status_display()})"
        )
