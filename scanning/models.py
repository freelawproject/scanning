import uuid

from django.conf import settings
from django.db import models


class Status(models.TextChoices):
    UPLOADED = "uploaded", "Uploaded"
    PROCESSING = "processing", "Processing"
    PENDING_REVIEW = "pending_review", "Pending Review"
    APPROVED = "approved", "Approved"
    EXTRACTED = "extracted", "Extracted"


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


def scan_upload_path(instance, filename):
    """Generate upload path for scan PDFs.

    :param instance: The Scan model instance.
    :type instance: Scan
    :param filename: The original filename.
    :type filename: str
    :returns: The upload path.
    :rtype: str
    """
    return f"uploads/{instance.reporter.short_name}/{instance.volume}/{uuid.uuid4()}.pdf"


def cover_upload_path(instance, filename):
    """Generate upload path for book cover images.

    :param instance: The Scan model instance.
    :type instance: Scan
    :param filename: The original filename.
    :type filename: str
    :returns: The upload path.
    :rtype: str
    """
    ext = filename.rsplit(".", 1)[-1] if "." in filename else "jpg"
    return (
        f"uploads/{instance.reporter.short_name}/{instance.volume}/"
        f"cover_{uuid.uuid4()}.{ext}"
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
    book_cover = models.ImageField(
        upload_to=cover_upload_path,
        blank=True,
    )
    original_pdf = models.FileField(
        upload_to=scan_upload_path,
    )
    redacted_pdf = models.FileField(
        null=True,
        blank=True,
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
            models.Index(
                fields=["uploaded_by"], name="idx_uploaded_by"
            ),
        ]
        ordering = ["-date_created"]

    def __str__(self):
        return (
            f"{self.reporter} vol. {self.volume} "
            f"({self.get_status_display()})"
        )
